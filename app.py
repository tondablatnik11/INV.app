import streamlit as st
import pandas as pd
import io
from datetime import timedelta

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher v2.0", page_icon="🔍", layout="wide")

st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #161b22; }
    h1 { color: #58a6ff !important; font-family: 'Inter', sans-serif; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; }
    .stButton>button { background-color: #238636; color: white; border-radius: 6px; width: 100%; }
    .match-success { color: #4caf50; font-weight: bold; }
    .match-fail { color: #f44336; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

st.title("🔍 Inventory Matcher v2.0")
st.markdown("Diagnostika a párování inventurních rozdílů.")

# --- 2. SIDEBAR ---
with st.sidebar:
    st.header("Vstupní data")
    file_inv = st.file_uploader("1. Inventurní rozdíly (INV.xlsx)", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("2. Export z LT24 (LT24.xlsx)", type=['xlsx', 'csv'])
    
    st.markdown("---")
    st.header("Nastavení párování")
    date_tolerance = st.checkbox("Povolit toleranci data ±1 den", value=True, help="Užitečné, pokud se potvrzení v LT24 a zaúčtování v INV liší o půlnoc.")

# --- 3. ROBUSTNÍ FUNKCE ---
def normalize_material(val):
    """Převede materiál na čistý string bez .0 a mezer."""
    if pd.isna(val): return ""
    s = str(val).strip()
    # Pokud excel načetl číslo jako float (např. 12345.0), odstraníme .0
    if s.endswith('.0'):
        s = s[:-2]
    return s

def normalize_date(val):
    """Bezpečný převod na date objekt."""
    if pd.isna(val): return None
    try:
        return pd.to_datetime(val).date()
    except:
        return None

def normalize_qty(val):
    """Absolutní hodnota float."""
    if pd.isna(val): return 0.0
    try:
        return abs(float(val))
    except:
        return 0.0

if file_inv and file_lt24:
    try:
        # Načtení dat
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # Očištění názvů sloupců (strip whitespace)
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        df_lt24.columns = [str(c).strip() for c in df_lt24.columns]

        # --- A. PŘÍPRAVA INV (Cíl) ---
        # Hledání klíčových sloupců
        inv_map = {
            'Mat': 'Material',
            'Qty': next((c for c in df_inv.columns if 'Menge' in c or 'Qty' in c), 'Menge in ErfassME'),
            'Date': next((c for c in df_inv.columns if 'datum' in c.lower() or 'Date' in c), 'Buchungsdatum')
        }
        
        df_inv['Key_Mat'] = df_inv[inv_map['Mat']].apply(normalize_material)
        df_inv['Key_Date'] = df_inv[inv_map['Date']].apply(normalize_date)
        df_inv['Key_Qty'] = df_inv[inv_map['Qty']].apply(normalize_qty)

        # --- B. PŘÍPRAVA LT24 (Zdroj) ---
        lt_map = {
            'Mat': 'Material',
            'Date': 'Confirmation date',
            'User': 'User',
            'Time': 'Confirmation time',
            'TO': 'Transfer Order Number'
        }
        
        # Hledání množství v LT24 (může být Source nebo Dest target qty)
        qty_cols_lt = [c for c in df_lt24.columns if 'target qty' in c.lower() or 'target quantity' in c.lower()]
        if not qty_cols_lt:
            st.error("V LT24 nebyl nalezen sloupec s množstvím (Source/Dest target qty).")
            st.stop()
            
        df_lt24['Key_Mat'] = df_lt24[lt_map['Mat']].apply(normalize_material)
        df_lt24['Key_Date'] = df_lt24[lt_map['Date']].apply(normalize_date)
        # Vezmeme max hodnotu z nalezených qty sloupců
        df_lt24['Key_Qty'] = df_lt24[qty_cols_lt].apply(lambda x: abs(pd.to_numeric(x, errors='coerce')).max(), axis=1).fillna(0)

        # Filtrujeme jen užitečné řádky z LT24 pro zrychlení
        lt24_pool = df_lt24[['Key_Mat', 'Key_Date', 'Key_Qty', lt_map['User'], lt_map['Time'], lt_map['TO']]].copy()
        lt24_pool['Used'] = False

        # --- C. DIAGNOSTIKA (Zobrazit náhled klíčů před párováním) ---
        with st.expander("🕵️ Diagnostika klíčů (Pokud se nic nepáruje, podívejte se sem)"):
            c1, c2 = st.columns(2)
            c1.write("**INV data (hledáme toto):**")
            c1.dataframe(df_inv[['Key_Mat', 'Key_Date', 'Key_Qty']].head())
            c2.write("**LT24 data (hledáme v tomto):**")
            c2.dataframe(lt24_pool[['Key_Mat', 'Key_Date', 'Key_Qty']].head())
            st.caption("Zkontrolujte, zda formáty Materiálu (např. nuly na začátku) a Data vypadají stejně.")

        # --- D. PÁROVÁNÍ ---
        results_user = []
        results_time = []
        results_to = []
        status_list = []

        progress_bar = st.progress(0)
        total = len(df_inv)

        for i, row in df_inv.iterrows():
            target_mat = row['Key_Mat']
            target_date = row['Key_Date']
            target_qty = row['Key_Qty']

            # Filtrování
            # 1. Shoda Materiálu a Množství
            candidates = lt24_pool[
                (lt24_pool['Key_Mat'] == target_mat) &
                (lt24_pool['Key_Qty'] == target_qty) &
                (lt24_pool['Used'] == False)
            ]

            # 2. Shoda Data (s tolerancí nebo bez)
            match = pd.DataFrame()
            if not candidates.empty:
                if date_tolerance and target_date:
                    # Datum ± 1 den
                    mask = (candidates['Key_Date'] >= target_date - timedelta(days=1)) & \
                           (candidates['Key_Date'] <= target_date + timedelta(days=1))
                    match = candidates[mask]
                else:
                    # Přesné datum
                    match = candidates[candidates['Key_Date'] == target_date]

            # Výsledek
            if not match.empty:
                # Našli jsme
                found = match.iloc[0]
                results_user.append(found[lt_map['User']])
                results_time.append(found[lt_map['Time']])
                results_to.append(found[lt_map['TO']])
                status_list.append("Nalezeno")
                
                # Označit jako použité
                lt24_pool.at[found.name, 'Used'] = True
            else:
                results_user.append("")
                results_time.append("")
                results_to.append("")
                status_list.append("Nenalezeno")
            
            if i % 20 == 0:
                progress_bar.progress(min((i + 1) / total, 1.0))
        
        progress_bar.empty()

        # Uložení do DF
        df_inv['User (LT24)'] = results_user
        df_inv['Time (LT24)'] = results_time
        df_inv['TO Number'] = results_to
        df_inv['Status'] = status_list
        df_inv['Důvod (Doplnit)'] = ""

        # --- E. VÝSLEDKY ---
        st.subheader("📊 Výsledek")
        found_cnt = status_list.count("Nalezeno")
        st.metric("Úspěšně spárováno", f"{found_cnt} / {total}", delta=f"{found_cnt/total:.0%}" if total else 0)

        # Zobrazit jen nespárované pro kontrolu
        if found_cnt < total:
            with st.expander("Zobrazit nespárované řádky"):
                st.dataframe(df_inv[df_inv['Status'] == "Nenalezeno"])

        # Export
        # Odstraníme pomocné klíče z exportu
        df_export = df_inv.drop(columns=['Key_Mat', 'Key_Date', 'Key_Qty'])

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_export.to_excel(writer, index=False, sheet_name="Matched_Inventory")
            ws = writer.sheets['Matched_Inventory']
            
            # Formátování
            fmt_yellow = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            
            try:
                col_u = df_export.columns.get_loc('User (LT24)')
                col_d = df_export.columns.get_loc('Důvod (Doplnit)')
                ws.set_column(col_u, col_u, 15, fmt_yellow)
                ws.set_column(col_d, col_d, 40, fmt_yellow)
            except:
                pass

        st.download_button("📥 Stáhnout Výsledek (.xlsx)", buffer.getvalue(), "Inventura_Sparovano.xlsx")

    except Exception as e:
        st.error(f"Chyba: {e}")
        st.write("Tip: Zkontrolujte sekci 'Diagnostika klíčů' výše.")
