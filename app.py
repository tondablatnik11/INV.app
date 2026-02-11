import streamlit as st
import pandas as pd
import io
from datetime import timedelta

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher v5.0", page_icon="🔬", layout="wide")

st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #161b22; }
    h1 { color: #58a6ff !important; font-family: 'Inter', sans-serif; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; }
    .stButton>button { background-color: #238636; color: white; border-radius: 6px; width: 100%; }
    .debug-box { background-color: #2b2d42; padding: 15px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #58a6ff; }
    </style>
    """, unsafe_allow_html=True)

st.title("🔬 Inventory Matcher v5.0 (Diagnostika)")
st.markdown("Pokud párování vrací 0, zde zjistíme proč.")

# --- 2. ČISTÍCÍ FUNKCE ---
def clean_material(val):
    """Agresivní čištění materiálu."""
    if pd.isna(val): return ""
    s = str(val).strip()
    # Odstranění .0 (pokud excel načetl číslo jako float)
    if s.endswith(".0"):
        s = s[:-2]
    return s.upper() # Pro jistotu vše velkým

def clean_qty(val):
    """Převod na absolutní float."""
    if pd.isna(val): return 0.0
    try:
        # Odstranění mezer a čárek
        s = str(val).replace(" ", "").replace(",", ".")
        return abs(float(s))
    except:
        return 0.0

def clean_date(val):
    """Převod na date objekt."""
    if pd.isna(val): return None
    try:
        return pd.to_datetime(val).date()
    except:
        return None

def determine_type(bin_val):
    """Logika pro Typ pohybu dle Dest.Storage Bin."""
    if pd.isna(bin_val): return ""
    s = str(bin_val).upper().strip()
    if "KORREKTUR" in s or "CORRECTION" in s:
        return "Manuální odpis"
    # Pokud to vypadá jako číslo (inventura)
    # Check if contains digits
    if any(char.isdigit() for char in s):
        return "Inventura"
    return "Jiný"

# --- 3. UI ---
with st.sidebar:
    st.header("1. Vstupní data")
    file_inv = st.file_uploader("INV.xlsx", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("LT24.xlsx", type=['xlsx', 'csv'])
    
    st.markdown("---")
    st.header("2. Nastavení")
    # Možnost vypnout párování podle data, často to způsobuje problémy
    use_date_matching = st.checkbox("Párovat i podle Data?", value=True)
    date_tol = st.checkbox("Tolerance ±1 den", value=True)

if file_inv and file_lt24:
    try:
        # NAČTENÍ
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # Normalizace názvů sloupců
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        df_lt24.columns = [str(c).strip() for c in df_lt24.columns]

        # --- DETEKCE SLOUPCŮ ---
        # INV
        c_inv_mat = 'Material'
        c_inv_qty = 'Menge in ErfassME'
        c_inv_date = 'Buchungsdatum'

        # LT24
        c_lt_mat = 'Material'
        c_lt_date = 'Confirmation date'
        c_lt_bin = 'Dest.Storage Bin'
        
        # Pro množství v LT24 zkusíme najít 'target qty'
        c_lt_qty_list = [c for c in df_lt24.columns if 'target' in c.lower() and 'qty' in c.lower()]
        if not c_lt_qty_list:
            st.error("Chyba: V LT24 nebyl nalezen sloupec s množstvím (Target Qty).")
            st.stop()
        
        # User a Time v LT24 (může jich být víc, vezmeme první co obsahuje User/Time)
        c_lt_user_list = [c for c in df_lt24.columns if 'user' in c.lower()]
        c_lt_time_list = [c for c in df_lt24.columns if 'time' in c.lower() and 'creation' not in c.lower()] # Ne creation time

        c_lt_user = c_lt_user_list[0] if c_lt_user_list else 'User'
        c_lt_time = c_lt_time_list[0] if c_lt_time_list else 'Confirmation time'

        # --- PŘÍPRAVA KLÍČŮ (RENTGEN) ---
        df_inv['K_Mat'] = df_inv[c_inv_mat].apply(clean_material)
        df_inv['K_Qty'] = df_inv[c_inv_qty].apply(clean_qty)
        df_inv['K_Date'] = df_inv[c_inv_date].apply(clean_date)

        df_lt24['K_Mat'] = df_lt24[c_lt_mat].apply(clean_material)
        df_lt24['K_Date'] = df_lt24[c_lt_date].apply(clean_date)
        
        # LT24 může mít množství v 'Source target qty' nebo 'Dest target qty'
        # Vezmeme maximum z nalezených sloupců pro každý řádek (jistota)
        df_lt24['K_Qty'] = df_lt24[c_lt_qty_list].apply(lambda x: x.apply(clean_qty).max(), axis=1)

        # --- DIAGNOSTIKA (Zobrazíme uživateli) ---
        st.markdown("### 🕵️ RENTGEN DAT (Zkontrolujte, zda se hodnoty shodují)")
        col1, col2 = st.columns(2)
        with col1:
            st.info("Co vidím v INV (Cíl):")
            st.dataframe(df_inv[['K_Mat', 'K_Qty', 'K_Date']].head(), use_container_width=True)
        with col2:
            st.info("Co vidím v LT24 (Zdroj):")
            st.dataframe(df_lt24[['K_Mat', 'K_Qty', 'K_Date']].head(), use_container_width=True)
        
        st.caption("Pokud vidíte vlevo '123' a vpravo '123.0', už by to mělo být opraveno. Pokud vidíte jiná data, je problém v souboru.")

        # --- PÁROVÁNÍ ---
        lt_pool = df_lt24.copy()
        lt_pool['Used'] = False
        
        results = {
            'User': [],
            'Time': [],
            'Type': [],
            'Status': []
        }

        # Progress bar
        prog = st.progress(0)
        total = len(df_inv)

        for i, row in df_inv.iterrows():
            mat = row['K_Mat']
            qty = row['K_Qty']
            date = row['K_Date']

            # 1. Základní filtr (Materiál + Množství + Nepoužité)
            candidates = lt_pool[
                (lt_pool['K_Mat'] == mat) &
                (lt_pool['K_Qty'] == qty) &
                (lt_pool['Used'] == False)
            ]

            # 2. Filtr data (pokud je zapnut)
            match = pd.DataFrame()
            if not candidates.empty:
                if use_date_matching and date:
                    if date_tolerance:
                        mask = (candidates['K_Date'] >= date - timedelta(days=1)) & \
                               (candidates['K_Date'] <= date + timedelta(days=1))
                        match = candidates[mask]
                    else:
                        match = candidates[candidates['K_Date'] == date]
                else:
                    match = candidates # Ignorujeme datum
            
            # 3. Zpracování
            if not match.empty:
                found = match.iloc[0] # Bereme první shodu
                
                # Zápis výsledků
                results['User'].append(found[c_lt_user])
                results['Time'].append(found[c_lt_time])
                
                # Logika typu
                bin_val = found[c_lt_bin] if c_lt_bin in found else ""
                results['Type'].append(determine_type(bin_val))
                results['Status'].append("Nalezeno")
                
                # Odškrtnout
                lt_pool.at[found.name, 'Used'] = True
            else:
                results['User'].append("")
                results['Time'].append("")
                results['Type'].append("")
                results['Status'].append("Nenalezeno")
            
            if i % 20 == 0: prog.progress(min((i+1)/total, 1.0))
        
        prog.empty()

        # --- VÝSLEDEK ---
        df_inv['User'] = results['User']
        df_inv['Čas'] = results['Time']
        df_inv['Typ pohybu'] = results['Type']
        df_inv['Důvod (Vyplnit)'] = ""
        
        # Vyčistit pomocné sloupce
        df_final = df_inv.drop(columns=['K_Mat', 'K_Qty', 'K_Date'])

        found_cnt = results['Status'].count("Nalezeno")
        
        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("Úspěšnost", f"{found_cnt} / {total}")
        
        if found_cnt == 0:
            st.error("❌ Stále 0? Zkuste vlevo odškrtnout 'Párovat i podle Data'.")
        else:
            st.success(f"✅ Podařilo se spárovat {found_cnt} řádků.")

        # --- EXPORT ---
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_final.to_excel(writer, index=False, sheet_name="Result")
            ws = writer.sheets['Result']
            
            # Formáty
            fmt_yellow = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            
            # Nastavení sloupců
            cols_to_highlight = ['User', 'Čas', 'Typ pohybu', 'Důvod (Vyplnit)']
            for idx, col_name in enumerate(df_final.columns):
                width = 15
                fmt = None
                if col_name in cols_to_highlight:
                    width = 25
                    fmt = fmt_yellow
                
                ws.set_column(idx, idx, width, fmt)

        st.download_button("📥 Stáhnout Excel", buffer.getvalue(), "Inventura_Hotovo.xlsx")

    except Exception as e:
        st.error(f"Chyba: {e}")

else:
    st.info("Nahrajte soubory pro spuštění.")
