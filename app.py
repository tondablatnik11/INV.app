import streamlit as st
import pandas as pd
import io

# --- 1. KONFIGURACE ---
st.set_page_config(page_title="Inventory Matcher", page_icon="🔍", layout="wide")

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

st.title("🔍 Inventory Matcher")
st.markdown("Doplnění uživatele a času k inventurním rozdílům z LT24.")

# --- 2. SIDEBAR ---
with st.sidebar:
    st.header("Vstupní data")
    file_inv = st.file_uploader("1. Inventurní rozdíly (INV.xlsx)", type=['xlsx', 'csv'])
    file_lt24 = st.file_uploader("2. Export z LT24 (LT24.xlsx)", type=['xlsx', 'csv'])
    st.info("Aplikace spáruje řádky na základě: Materiálu, Data a Množství.")

# --- 3. LOGIKA PÁROVÁNÍ ---
def clean_material(val):
    """Odstraní nuly na začátku a převede na string pro lepší párování."""
    if pd.isna(val): return ""
    return str(val).strip()

def normalize_date(val):
    """Převede datum na standardní datetime.date object."""
    if pd.isna(val): return None
    try:
        return pd.to_datetime(val).date()
    except:
        return None

if file_inv and file_lt24:
    try:
        # Načtení dat
        df_inv = pd.read_csv(file_inv) if file_inv.name.endswith('.csv') else pd.read_excel(file_inv)
        df_lt24 = pd.read_csv(file_lt24) if file_lt24.name.endswith('.csv') else pd.read_excel(file_lt24)

        # --- PŘÍPRAVA INV (Cíl) ---
        # Očekávané sloupce v INV: 'Material', 'Menge in ErfassME', 'Buchungsdatum'
        # Pokud se jmenují jinak, pokusíme se je najít
        col_mat_inv = 'Material'
        col_qty_inv = 'Menge in ErfassME'
        col_date_inv = 'Buchungsdatum'

        # Převody pro párování
        df_inv['Match_Mat'] = df_inv[col_mat_inv].apply(clean_material)
        df_inv['Match_Date'] = df_inv[col_date_inv].apply(normalize_date)
        df_inv['Match_Qty'] = df_inv[col_qty_inv].abs() # Absolutní hodnota (ignorováni znaménka)

        # --- PŘÍPRAVA LT24 (Zdroj) ---
        # Očekávané sloupce: 'Material', 'Confirmation date', 'User', 'Confirmation time', 'Source target qty'
        col_mat_lt = 'Material'
        col_date_lt = 'Confirmation date'
        
        # Množství v LT24 může být ve více sloupcích, vezmeme 'Source target qty' nebo 'Dest.target quantity'
        # Vytvoříme pomocný sloupec s max hodnotou množství na řádku
        qty_cols_lt = [c for c in df_lt24.columns if 'target qty' in c.lower() or 'target quantity' in c.lower()]
        if not qty_cols_lt:
            st.error("V souboru LT24 nebyl nalezen sloupec s množstvím (Target Qty).")
            st.stop()
            
        df_lt24['Match_Mat'] = df_lt24[col_mat_lt].apply(clean_material)
        df_lt24['Match_Date'] = df_lt24[col_date_lt].apply(normalize_date)
        # Vezme maximální množství z nalezených sloupců (obvykle jedno je 0 a druhé je hodnota)
        df_lt24['Match_Qty'] = df_lt24[qty_cols_lt].max(axis=1)

        # Vybereme jen potřebné sloupce z LT24 pro zrychlení a vytvoříme kopii
        lt24_pool = df_lt24[['Match_Mat', 'Match_Date', 'Match_Qty', 'User', 'Confirmation time', 'Transfer Order Number']].copy()
        
        # Přidáme sloupec 'Used' do LT24, abychom nepoužili stejný záznam 2x pro různé řádky v INV
        lt24_pool['Used'] = False

        # --- VLASTNÍ ALGORITMUS PÁROVÁNÍ ---
        # Nemůžeme použít jednoduchý merge, protože můžeme mít 3 stejné odpisy ve stejný den.
        # Musíme iterovat a "odškrtávat" použité řádky z LT24.
        
        results_user = []
        results_time = []
        results_to = []
        status = []

        # Progress bar
        progress_bar = st.progress(0)
        total_rows = len(df_inv)

        for index, row in df_inv.iterrows():
            # Filtrujeme LT24 podle shody Materiálu, Data a Množství
            # A zároveň nesmí být už použitý ('Used' == False)
            match = lt24_pool[
                (lt24_pool['Match_Mat'] == row['Match_Mat']) &
                (lt24_pool['Match_Date'] == row['Match_Date']) &
                (lt24_pool['Match_Qty'] == row['Match_Qty']) &
                (lt24_pool['Used'] == False)
            ]

            if not match.empty:
                # Našli jsme shodu (vezmeme první nalezený záznam)
                found_row = match.iloc[0]
                results_user.append(found_row['User'])
                results_time.append(found_row['Confirmation time'])
                results_to.append(found_row['Transfer Order Number'])
                status.append("Nalezeno")
                
                # Označíme v poolu jako použité (podle indexu původního LT24 poolu)
                lt24_pool.at[found_row.name, 'Used'] = True
            else:
                # Nenašli jsme shodu
                results_user.append("Nenalezeno")
                results_time.append("")
                results_to.append("")
                status.append("Chybí v LT24")
            
            if index % 10 == 0:
                progress_bar.progress(min((index + 1) / total_rows, 1.0))
        
        progress_bar.empty()

        # Zapsání výsledků do DF
        df_inv['User (LT24)'] = results_user
        df_inv['Time (LT24)'] = results_time
        df_inv['TO Number'] = results_to
        df_inv['Status'] = status
        
        # Přidání prázdného sloupce pro Důvod (aby ho uživatel mohl doplnit v Excelu)
        df_inv['Důvod (Doplnit)'] = ""

        # Úklid pomocných sloupců
        df_final = df_inv.drop(columns=['Match_Mat', 'Match_Date', 'Match_Qty'])

        # --- VÝSLEDKY ---
        st.subheader("📊 Výsledek párování")
        
        found_count = status.count("Nalezeno")
        missing_count = status.count("Chybí v LT24")
        
        c1, c2 = st.columns(2)
        c1.metric("Úspěšně spárováno", found_count)
        c2.metric("Nenalezeno", missing_count, delta_color="inverse")

        st.dataframe(
            df_final, 
            use_container_width=True,
            column_config={
                "Status": st.column_config.TextColumn(
                    "Stav",
                    help="Výsledek hledání v LT24",
                    width="medium",
                ),
            }
        )

        # --- EXPORT ---
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_final.to_excel(writer, index=False, sheet_name="Inventory_Matched")
            ws = writer.sheets['Inventory_Matched']
            
            # Formátování
            # Zvýraznění sloupce User a Důvod
            format_yellow = writer.book.add_format({'bg_color': '#FFF9C4', 'border': 1})
            format_header = writer.book.add_format({'bold': True, 'border': 1})
            
            # Najdeme indexy sloupců
            user_col_idx = df_final.columns.get_loc('User (LT24)')
            reason_col_idx = df_final.columns.get_loc('Důvod (Doplnit)')
            
            ws.set_column(user_col_idx, user_col_idx, 20, format_yellow)
            ws.set_column(reason_col_idx, reason_col_idx, 40, format_yellow)
            
            # Auto-fit (zjednodušený)
            for i, col in enumerate(df_final.columns):
                ws.set_column(i, i, 20)

        st.download_button(
            label="📥 Stáhnout Spárovaný Excel",
            data=buffer.getvalue(),
            file_name="Inventura_Doplneno.xlsx",
            mime="application/vnd.ms-excel"
        )

    except Exception as e:
        st.error(f"Chyba při zpracování: {e}")
        st.write("Zkontrolujte, zda soubory mají správnou strukturu (sloupce Material, Buchungsdatum/Confirmation date atd.)")

else:
    st.info("Nahrajte prosím oba soubory (INV.xlsx a LT24.xlsx).")
