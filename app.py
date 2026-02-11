import streamlit as st
import pandas as pd
import io
from datetime import timedelta

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher v6.0", page_icon="🕵️", layout="wide")

st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #161b22; }
    h1 { color: #58a6ff !important; font-family: 'Inter', sans-serif; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; }
    .stButton>button { background-color: #238636; color: white; border-radius: 6px; width: 100%; }
    </style>
    """, unsafe_allow_html=True)

st.title("🕵️ Inventory Matcher v6.0 (User Fix)")
st.markdown("Oprava načítání uživatelů a párování plusových/mínusových položek.")

# --- 2. ČISTÍCÍ FUNKCE ---
def normalize_material(val):
    if pd.isna(val): return ""
    s = str(val).strip()
    if s.endswith(".0"): s = s[:-2]
    return s.upper()

def normalize_date(val):
    if pd.isna(val): return None
    try: return pd.to_datetime(val).date()
    except: return None

def normalize_qty(val):
    if pd.isna(val): return 0.0
    try:
        s = str(val).replace(",", ".").replace(" ", "")
        return abs(float(s))
    except: return 0.0

def get_smart_user(row, user_cols):
    """Projdu všechny sloupce s názvem 'User' a vrátím první neprázdný."""
    for col in user_cols:
        val = row[col]
        if pd.notna(val) and str(val).strip() != "":
            return str(val).strip()
    return "Neznámý"

def determine_type(bin_val):
    if pd.isna(bin_val): return ""
    s = str(bin_val).upper().strip()
    if "KORREKTUR" in s or "CORRECTION" in s: return "Manuální odpis"
    if any(char.isdigit() for char in s): return "Inventura"
    return "Jiný"

# --- 3. UI ---
with st.sidebar:
    st.header("Vstupní data")
    file_inv = st.file_uploader("INV.xlsx", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("LT24.xlsx", type=['xlsx', 'csv'])
    
    st.markdown("---")
    date_tolerance = st.checkbox("Tolerance data ±1 den", value=True)

if file_inv and file_lt24:
    try:
        # NAČTENÍ
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # Očištění názvů sloupců
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        df_lt24.columns = [str(c).strip() for c in df_lt24.columns]

        # --- DETEKCE SLOUPCŮ ---
        # 1. Množství v LT24 (Vezmeme max ze všech target qty sloupců pro každý řádek)
        # To řeší problém, zda je množství v Source nebo Dest sloupci
        qty_cols_lt = [c for c in df_lt24.columns if 'target' in c.lower() and 'qty' in c.lower()]
        if not qty_cols_lt:
            st.error("Chyba: V LT24 chybí sloupce s množstvím (Target Qty).")
            st.stop()
        
        # 2. User sloupce v LT24 (Všechny, co obsahují "User")
        user_cols_lt = [c for c in df_lt24.columns if 'user' in c.lower()]
        
        # 3. Time sloupce
        time_cols_lt = [c for c in df_lt24.columns if 'time' in c.lower() and 'creation' not in c.lower()]
        col_lt_time = time_cols_lt[0] if time_cols_lt else 'Confirmation time'
        
        # 4. Storage Bin
        col_lt_bin = 'Dest.Storage Bin' if 'Dest.Storage Bin' in df_lt24.columns else df_lt24.columns[6] # Fallback

        # --- PŘÍPRAVA KLÍČŮ ---
        # INV
        df_inv['K_Mat'] = df_inv['Material'].apply(normalize_material)
        df_inv['K_Date'] = df_inv['Buchungsdatum'].apply(normalize_date)
        df_inv['K_Qty'] = df_inv['Menge in ErfassME'].apply(normalize_qty) # Absolutní hodnota

        # LT24
        df_lt24['K_Mat'] = df_lt24['Material'].apply(normalize_material)
        df_lt24['K_Date'] = df_lt24['Confirmation date'].apply(normalize_date)
        # Vypočítáme maximální množství na řádku (abychom chytili správné číslo nezávisle na sloupci)
        df_lt24['K_Qty'] = df_lt24[qty_cols_lt].apply(lambda x: x.apply(normalize_qty).max(), axis=1)

        # LT24 Pool
        lt_pool = df_lt24.copy()
        lt_pool['Used'] = False

        # --- PÁROVÁNÍ ---
        results = {'User': [], 'Time': [], 'Type': [], 'Status': []}
        
        prog = st.progress(0)
        total = len(df_inv)

        for i, row in df_inv.iterrows():
            mat = row['K_Mat']
            qty = row['K_Qty']
            date = row['K_Date']

            # 1. Najít kandidáty (Shoda Mat, Qty a Nepoužité)
            # Tady seQty == Qty postará o shodu (protože máme absolutní hodnoty na obou stranách)
            candidates = lt_pool[
                (lt_pool['K_Mat'] == mat) &
                (lt_pool['K_Qty'] == qty) &
                (lt_pool['Used'] == False)
            ]

            match = pd.DataFrame()

            # 2. Filtr Data
            if not candidates.empty:
                if date:
                    if date_tolerance:
                        start = date - timedelta(days=1)
                        end = date + timedelta(days=1)
                        match = candidates[(candidates['K_Date'] >= start) & (candidates['K_Date'] <= end)]
                    else:
                        match = candidates[candidates['K_Date'] == date]
                else:
                    match = candidates # Bez data
            
            # 3. Výsledek
            if not match.empty:
                found = match.iloc[0]
                
                # Získání Usera (iterace přes všechny user sloupce)
                u_val = get_smart_user(found, user_cols_lt)
                t_val = found[col_lt_time] if col_lt_time in found else ""
                b_val = found[col_lt_bin] if col_lt_bin in found else ""
                
                results['User'].append(u_val)
                results['Time'].append(t_val)
                results['Type'].append(determine_type(b_val))
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

        # --- VÝSTUP ---
        df_inv['User'] = results['User']
        df_inv['Čas'] = results['Time']
        df_inv['Typ pohybu'] = results['Type']
        df_inv['Důvod (Vyplnit)'] = ""
        
        final_df = df_inv.drop(columns=['K_Mat', 'K_Date', 'K_Qty'])
        
        found_cnt = results['Status'].count("Nalezeno")
        
        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("Spárováno", f"{found_cnt} / {total}")
        
        # Zobrazit náhled, pokud je User prázdný u nalezených
        if found_cnt > 0:
            empty_users = final_df[(final_df['Status']=="Nalezeno") & (final_df['User']=="Neznámý")]
            if not empty_users.empty:
                st.warning(f"Pozor: U {len(empty_users)} řádků byla nalezena shoda, ale sloupec User je prázdný. Zkontrolujte LT24.")

        # --- EXPORT ---
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_df.to_excel(writer, index=False, sheet_name="Final_Match")
            ws = writer.sheets['Final_Match']
            
            fmt_yellow = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            
            # Nastavení šířky a barev
            target_cols = ['User', 'Čas', 'Typ pohybu', 'Důvod (Vyplnit)']
            for idx, col in enumerate(final_df.columns):
                width = 15
                fmt = None
                if col in target_cols:
                    width = 25
                    fmt = fmt_yellow
                ws.set_column(idx, idx, width, fmt)

        st.download_button("📥 Stáhnout Opravený Excel", buffer.getvalue(), "Inventura_UserFix.xlsx")

    except Exception as e:
        st.error(f"Chyba: {e}")

else:
    st.info("Nahrajte soubory.")
