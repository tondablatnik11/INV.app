import streamlit as st
import pandas as pd
import io
from datetime import timedelta

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher v8.0", page_icon="📆", layout="wide")

st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #161b22; }
    h1 { color: #58a6ff !important; font-family: 'Inter', sans-serif; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; }
    .stButton>button { background-color: #238636; color: white; border-radius: 6px; width: 100%; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

st.title("📆 Inventory Matcher v8.0")
st.markdown("Párování podle **Creation Date** (Datum vytvoření).")

# --- 2. FUNKCE PRO ČIŠTĚNÍ ---
def super_clean_mat(val):
    """Převede na string, odstraní mezery, nuly na začátku i konci, tečky."""
    if pd.isna(val): return "MISSING"
    s = str(val).upper().strip()
    if s.endswith(".0"): s = s[:-2]
    s = s.replace(" ", "")
    return s

def super_clean_qty(val):
    """Absolutní hodnota float."""
    if pd.isna(val): return 0.0
    try:
        s = str(val).replace(",", ".").replace(" ", "")
        return abs(float(s))
    except:
        return 0.0

def super_clean_date(val):
    if pd.isna(val): return None
    try:
        return pd.to_datetime(val).date()
    except:
        return None

def determine_type(bin_val):
    if pd.isna(bin_val): return ""
    s = str(bin_val).upper().strip()
    if "KORREKTUR" in s or "CORRECTION" in s: return "Manuální odpis"
    if any(char.isdigit() for char in s): return "Inventura"
    return "Jiný"

def get_smart_user(row, columns):
    """Najde první neprázdný sloupec s názvem User."""
    for col in columns:
        if 'user' in col.lower():
            val = row[col]
            if pd.notna(val) and str(val).strip() != "":
                return str(val)
    return ""

# --- 3. UI APLIKACE ---
with st.sidebar:
    st.header("1. Vstupní data")
    file_inv = st.file_uploader("INV.xlsx", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("LT24.xlsx", type=['xlsx', 'csv'])
    
    st.markdown("---")
    st.info("Nyní se v LT24 používá 'Creation Date'.")

if file_inv and file_lt24:
    try:
        # NAČTENÍ
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # Očištění názvů sloupců
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        df_lt24.columns = [str(c).strip() for c in df_lt24.columns]

        # --- DETEKCE SLOUPCŮ ---
        # INV
        col_inv_mat = 'Material'
        col_inv_qty = 'Menge in ErfassME'
        col_inv_date = 'Buchungsdatum'

        # LT24 - ZMĚNA: Hledáme Creation Date
        # Zkusíme najít přesný název nebo něco podobného
        col_lt_date = None
        for c in df_lt24.columns:
            if "creation" in c.lower() and "date" in c.lower():
                col_lt_date = c
                break
        
        if not col_lt_date:
            st.error("CHYBA: V LT24 nebyl nalezen sloupec 'Creation Date'. Zkontrolujte soubor.")
            st.stop()
        else:
            st.sidebar.success(f"LT24 Datum: {col_lt_date}")

        col_lt_mat = 'Material'
        col_lt_bin = 'Dest.Storage Bin' if 'Dest.Storage Bin' in df_lt24.columns else df_lt24.columns[6] # Fallback

        # Množství v LT24 (Target Qty)
        qty_cols = [c for c in df_lt24.columns if 'target' in c.lower() and 'qty' in c.lower()]
        if not qty_cols:
             st.error("CHYBA: V LT24 chybí sloupce 'Target Qty'.")
             st.stop()

        # --- PŘÍPRAVA KLÍČŮ ---
        df_inv['MATCH_MAT'] = df_inv[col_inv_mat].apply(super_clean_mat)
        df_inv['MATCH_QTY'] = df_inv[col_inv_qty].apply(super_clean_qty)
        df_inv['MATCH_DATE'] = df_inv[col_inv_date].apply(super_clean_date)

        df_lt24['MATCH_MAT'] = df_lt24[col_lt_mat].apply(super_clean_mat)
        df_lt24['MATCH_DATE'] = df_lt24[col_lt_date].apply(super_clean_date)
        # Max množství z target sloupců
        df_lt24['MATCH_QTY'] = df_lt24[qty_cols].apply(lambda x: x.apply(super_clean_qty).max(), axis=1)

        # --- DIAGNOSTIKA ---
        with st.expander("🕵️ RENTGEN DAT (Klikni pro kontrolu)", expanded=False):
            c1, c2 = st.columns(2)
            c1.write("### INV (Cíl)")
            c1.dataframe(df_inv[['MATCH_MAT', 'MATCH_QTY', 'MATCH_DATE']].head())
            c2.write("### LT24 (Zdroj)")
            c2.dataframe(df_lt24[['MATCH_MAT', 'MATCH_QTY', 'MATCH_DATE']].head())

        # --- PÁROVÁNÍ ---
        lt_pool = df_lt24.copy()
        lt_pool['Used'] = False
        
        results_user = []
        results_time = []
        results_type = []
        status_list = []
        
        prog = st.progress(0)
        total = len(df_inv)
        matches = 0

        for i, row in df_inv.iterrows():
            mat = row['MATCH_MAT']
            qty = row['MATCH_QTY']
            date = row['MATCH_DATE']

            # 1. Shoda Mat + Qty + Nepoužité
            candidates = lt_pool[
                (lt_pool['MATCH_MAT'] == mat) & 
                (lt_pool['MATCH_QTY'] == qty) & 
                (lt_pool['Used'] == False)
            ]

            final_match = pd.DataFrame()
            found_status = "Nenalezeno"

            if not candidates.empty:
                # 2. Shoda Datum ± 1 den (nyní podle Creation Date)
                if date:
                    start = date - timedelta(days=1)
                    end = date + timedelta(days=1)
                    date_match = candidates[(candidates['MATCH_DATE'] >= start) & (candidates['MATCH_DATE'] <= end)]
                    
                    if not date_match.empty:
                        final_match = date_match.iloc[[0]]
                        found_status = "Nalezeno"
                    else:
                        # Datum nesedí, ale materiál ano -> Bereme to jako "částečnou shodu" nebo nic?
                        # Zde pro jistotu bereme i shodu bez data, pokud je to jediná možnost
                        # (Můžete zakomentovat, pokud chcete striktní datum)
                        final_match = candidates.iloc[[0]] 
                        found_status = "Nalezeno (Datum nesedí)"
                else:
                     final_match = candidates.iloc[[0]]
                     found_status = "Nalezeno"

            if not final_match.empty:
                found = final_match.iloc[0]
                
                # Získání dat
                u = get_smart_user(found, df_lt24.columns)
                
                # Čas (obvykle Creation time nebo Confirmation time)
                # Zkusíme Creation time, pokud tam je
                t_cols = [c for c in df_lt24.columns if 'time' in c.lower()]
                t = found[t_cols[0]] if t_cols else ""
                
                b = found[col_lt_bin] if col_lt_bin in found else ""
                
                results_user.append(u)
                results_time.append(t)
                results_type.append(determine_type(b))
                status_list.append(found_status)
                
                lt_pool.at[found.name, 'Used'] = True
                matches += 1
            else:
                results_user.append("")
                results_time.append("")
                results_type.append("")
                status_list.append("Nenalezeno")
            
            if i % 20 == 0: prog.progress(min((i+1)/total, 1.0))
        
        prog.empty()

        # --- VÝSTUP ---
        df_inv['User'] = results_user
        df_inv['Čas'] = results_time
        df_inv['Typ pohybu'] = results_type
        df_inv['Status'] = status_list
        df_inv['Důvod (Vyplnit)'] = ""

        # Úklid
        final_df = df_inv.drop(columns=['MATCH_MAT', 'MATCH_QTY', 'MATCH_DATE'])

        st.divider()
        st.metric("✅ Spárováno", f"{matches} / {total}")

        # EXPORT
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_df.to_excel(writer, index=False, sheet_name="Result")
            ws = writer.sheets['Result']
            
            yellow = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            
            for idx, col in enumerate(final_df.columns):
                width = 15
                fmt = None
                if col in ['User', 'Čas', 'Typ pohybu', 'Důvod (Vyplnit)']:
                    width = 25
                    fmt = yellow
                ws.set_column(idx, idx, width, fmt)

        st.download_button("📥 Stáhnout Excel", buffer.getvalue(), "Inventura_CreationDate.xlsx")

    except Exception as e:
        st.error(f"Chyba: {e}")

else:
    st.info("Nahrajte soubory.")
