import streamlit as st
import pandas as pd
import io
from datetime import timedelta

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher v4.0", page_icon="📦", layout="wide")

st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #161b22; }
    h1 { color: #58a6ff !important; font-family: 'Inter', sans-serif; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; }
    .stButton>button { background-color: #238636; color: white; border-radius: 6px; width: 100%; }
    </style>
    """, unsafe_allow_html=True)

st.title("📦 Inventory Matcher v4.0")
st.markdown("Párování inventurních rozdílů s detekcí typu (Inventura / Manuální odpis).")

# --- 2. POMOCNÉ FUNKCE PRO ČIŠTĚNÍ DAT ---
def normalize_material(val):
    """Převede materiál na čistý text (odstraní .0 a mezery)."""
    if pd.isna(val): return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s

def normalize_date(val):
    """Převede na objekt data (pro porovnání)."""
    if pd.isna(val): return None
    try:
        return pd.to_datetime(val).date()
    except:
        return None

def normalize_qty(val):
    """Vrátí absolutní hodnotu jako float (pro porovnání -10 a 10)."""
    if pd.isna(val): return 0.0
    try:
        val = str(val).replace(',', '').replace(' ', '')
        return abs(float(val))
    except:
        return 0.0

def determine_type(bin_value):
    """Určí typ pohybu na základě Dest.Storage Bin."""
    if pd.isna(bin_value): return "Neznámý"
    val = str(bin_value).upper().strip()
    
    if "KORREKTUR" in val or "CORRECTION" in val:
        return "Manuální odpis"
    elif val.isdigit() or (val.startswith("0") and len(val) > 5): 
        # Pokud je to číslo (např. 0000005194), je to inventura
        return "Inventura"
    else:
        return f"Jiný ({val})"

# --- 3. UI APLIKACE ---
with st.sidebar:
    st.header("1. Vstupní data")
    file_inv = st.file_uploader("Nahrajte INV.xlsx", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("Nahrajte LT24.xlsx", type=['xlsx', 'csv'])
    
    st.markdown("---")
    st.header("2. Nastavení")
    date_tolerance = st.checkbox("Tolerance data ±1 den", value=True, help="Zapnout, pokud se data potvrzení a zaúčtování mohou mírně lišit.")

if file_inv and file_lt24:
    try:
        # NAČTENÍ DAT
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # Očištění názvů sloupců
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        df_lt24.columns = [str(c).strip() for c in df_lt24.columns]

        # --- AUTOMATICKÁ DETEKCE SLOUPCŮ ---
        # INV
        col_inv_mat = 'Material'
        col_inv_date = 'Buchungsdatum'
        col_inv_qty = 'Menge in ErfassME' # Hledáme tento nebo podobný
        
        # LT24
        col_lt_mat = 'Material'
        col_lt_date = 'Confirmation date'
        col_lt_user = 'User'
        col_lt_time = 'Confirmation time'
        col_lt_bin = 'Dest.Storage Bin'
        
        # Hledání množství v LT24 (Source target qty nebo Dest target qty)
        col_lt_qty = None
        for c in df_lt24.columns:
            if 'target' in c.lower() and 'qty' in c.lower() and 'source' in c.lower():
                col_lt_qty = c
                break
        if not col_lt_qty: # Fallback
             for c in df_lt24.columns:
                if 'target' in c.lower() and 'qty' in c.lower():
                    col_lt_qty = c
                    break

        # Kontrola, zda sloupce existují
        missing_cols = []
        if col_inv_qty not in df_inv.columns: missing_cols.append(f"INV: {col_inv_qty}")
        if col_lt_qty not in df_lt24.columns: missing_cols.append(f"LT24: Množství (Target Qty)")
        if col_lt_bin not in df_lt24.columns: missing_cols.append(f"LT24: {col_lt_bin}")

        if missing_cols:
            st.error(f"Chybí tyto sloupce: {', '.join(missing_cols)}")
            st.stop()

        # --- PŘÍPRAVA KLÍČŮ ---
        # Vytvoříme dočasné sloupce pro přesné párování
        df_inv['K_Mat'] = df_inv[col_inv_mat].apply(normalize_material)
        df_inv['K_Qty'] = df_inv[col_inv_qty].apply(normalize_qty)
        df_inv['K_Date'] = df_inv[col_inv_date].apply(normalize_date)

        df_lt24['K_Mat'] = df_lt24[col_lt_mat].apply(normalize_material)
        df_lt24['K_Qty'] = df_lt24[col_lt_qty].apply(normalize_qty)
        df_lt24['K_Date'] = df_lt24[col_lt_date].apply(normalize_date)

        # LT24 Pool - vytvoříme kopii pro "odškrtávání" použitých
        lt_pool = df_lt24.copy()
        lt_pool['Used'] = False

        # --- HLAVNÍ LOOP (Zachování počtu řádků INV) ---
        results_user = []
        results_time = []
        results_type = [] # Inventura vs Manuální
        status_list = []

        progress_bar = st.progress(0)
        total_rows = len(df_inv)

        for i, row in df_inv.iterrows():
            target_mat = row['K_Mat']
            target_qty = row['K_Qty']
            target_date = row['K_Date']

            # 1. Filtrujeme kandidáty v LT24 (Shoda Mat, Qty a Nepoužité)
            candidates = lt_pool[
                (lt_pool['K_Mat'] == target_mat) &
                (lt_pool['K_Qty'] == target_qty) &
                (lt_pool['Used'] == False)
            ]

            match_found = pd.DataFrame()

            # 2. Filtrujeme podle Data (s tolerancí)
            if not candidates.empty:
                if target_date:
                    if date_tolerance:
                        start_date = target_date - timedelta(days=1)
                        end_date = target_date + timedelta(days=1)
                        match_found = candidates[(candidates['K_Date'] >= start_date) & (candidates['K_Date'] <= end_date)]
                    else:
                        match_found = candidates[candidates['K_Date'] == target_date]
                else:
                    match_found = candidates # Pokud v INV chybí datum, zkusíme vzít jakoukoliv shodu materiálu/množství

            # 3. Zpracování výsledku
            if not match_found.empty:
                # Vezmeme první shodu
                found = match_found.iloc[0]
                
                # Získání dat
                user = found[col_lt_user]
                time_val = found[col_lt_time]
                bin_val = found[col_lt_bin]
                
                # Logika Typu (KORREKTUR vs Inventura)
                type_val = determine_type(bin_val)

                results_user.append(user)
                results_time.append(time_val)
                results_type.append(type_val)
                status_list.append("Nalezeno")

                # Označíme řádek v LT24 jako POUŽITÝ, aby se nepřiřadil jinému řádku v INV
                lt_pool.at[found.name, 'Used'] = True
            else:
                # Nenalezeno
                results_user.append("")
                results_time.append("")
                results_type.append("")
                status_list.append("Nenalezeno")
            
            if i % 20 == 0:
                progress_bar.progress(min((i + 1) / total_rows, 1.0))
        
        progress_bar.empty()

        # --- SESTAVENÍ VÝSLEDKU ---
        # Přidáme nové sloupce do původního INV datasetu
        df_inv['User'] = results_user
        df_inv['Čas'] = results_time
        df_inv['Typ pohybu'] = results_type
        df_inv['Důvod (Vyplnit)'] = "" # Prázdný sloupec pro manuální input

        # Odstranění pomocných klíčů pro čistý export
        df_final = df_inv.drop(columns=['K_Mat', 'K_Qty', 'K_Date'])

        # --- ZOBRAZENÍ ---
        st.subheader("📊 Výsledky párování")
        
        found_count = status_list.count("Nalezeno")
        st.metric("Spárováno položek", f"{found_count} / {total_rows}")

        # Náhled
        st.dataframe(df_final.head(10), use_container_width=True)

        if found_count == 0:
            st.warning("⚠️ Žádná shoda nebyla nalezena. Zkontrolujte, zda 'Menge in ErfassME' v INV odpovídá 'Source target qty' v LT24 (absolutní hodnotou).")

        # --- EXPORT ---
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_final.to_excel(writer, index=False, sheet_name="Inventory_Check")
            ws = writer.sheets['Inventory_Check']
            
            # Formátování (Žluté pole pro Důvod a Usera)
            yellow_fmt = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            header_fmt = writer.book.add_format({'bold': True, 'border': 1})
            
            # Nastavení šířky sloupců
            for idx, col in enumerate(df_final.columns):
                width = 15
                if col == "User": width = 20
                if col == "Typ pohybu": width = 25
                if col == "Důvod (Vyplnit)": width = 50
                
                # Zvýraznění sloupců, které nás zajímají
                if col in ["User", "Čas", "Typ pohybu", "Důvod (Vyplnit)"]:
                    ws.set_column(idx, idx, width, yellow_fmt)
                else:
                    ws.set_column(idx, idx, width)

        st.download_button(
            label="📥 Stáhnout Hotový Excel",
            data=buffer.getvalue(),
            file_name="Inventura_Doplneno.xlsx",
            mime="application/vnd.ms-excel"
        )

    except Exception as e:
        st.error(f"Chyba: {e}")
        st.write("Prosím zkontrolujte, zda názvy sloupců přesně odpovídají zadání (Material, Confirmation date, Dest.Storage Bin...).")

else:
    st.info("Nahrajte soubory INV.xlsx a LT24.xlsx pro spuštění.")
