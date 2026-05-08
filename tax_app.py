import streamlit as st
import pdfplumber
import pandas as pd
import re
from datetime import datetime
from supabase import create_client, Client
import os

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Taxalytics",
    page_icon="📊",
    layout="wide"
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}
h1, h2, h3 {
    font-family: 'IBM Plex Mono', monospace;
}
.stApp { background-color: #F7F6F1; }

.header-bar {
    background: #1A1A2E;
    color: #E8E0D0;
    padding: 1.2rem 2rem;
    border-radius: 8px;
    margin-bottom: 1.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
}
.header-bar h1 { color: #F5C842; margin: 0; font-size: 1.4rem; }
.header-bar p  { color: #A0A8C0; margin: 0; font-size: 0.85rem; }

.card {
    background: #FFFFFF;
    border: 1px solid #E0DDD5;
    border-radius: 8px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1rem;
}
.card-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.8rem;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.5rem;
}
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-new  { background: #D4EDDA; color: #155724; }
.badge-old  { background: #FFF3CD; color: #856404; }
.badge-ok   { background: #CCE5FF; color: #004085; }

.summary-row {
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
}
.summary-card {
    flex: 1;
    background: #1A1A2E;
    color: #E8E0D0;
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
}
.summary-card .label { font-size: 0.75rem; color: #A0A8C0; }
.summary-card .value { font-size: 1.3rem; font-weight: 600; color: #F5C842; font-family: 'IBM Plex Mono', monospace; }
</style>
""", unsafe_allow_html=True)

# ── Supabase connection ───────────────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase = get_supabase()

# ── Helper: fetch clients ─────────────────────────────────────────────────────
def fetch_clients():
    res = supabase.table("clients").select("*").order("name").execute()
    return res.data or []

# ── Helper: insert client ─────────────────────────────────────────────────────
def insert_client(pan: str, name: str) -> int:
    res = supabase.table("clients").insert({"pan": pan.upper(), "name": name}).execute()
    return res.data[0]["id"]

# ── Helper: check duplicate AY ────────────────────────────────────────────────
def ay_exists(client_id: int, assessment_year: str) -> bool:
    res = (supabase.table("form26as_header")
           .select("id")
           .eq("client_id", client_id)
           .eq("assessment_year", assessment_year)
           .execute())
    return len(res.data) > 0

# ══════════════════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════════════════

def detect_format(text: str) -> str:
    """Detect OLD (pre-2021) vs NEW format based on part labels in PDF text."""
    if "PART-I" in text or "PART I" in text or "Part-I" in text:
        return "NEW"
    if "PART A" in text or "Part A" in text:
        return "OLD"
    return "NEW"  # default to new

def extract_header(text: str) -> dict:
    """Extract PAN, name, financial year, assessment year."""
    pan   = re.search(r'PAN[)\s]*([A-Z]{5}[0-9]{4}[A-Z])', text)
    fy    = re.search(r'Financial Year[:\s]+(\d{4}-\d{2,4})', text)
    ay    = re.search(r'Assessment Year[:\s]+(\d{4}-\d{2,4})', text)
    name  = re.search(r'Name of Assessee\s+([A-Z ]+)', text)
    addr  = re.search(r'Address of Assessee\s+(.+?)(?=Above data|$)', text, re.DOTALL)
    updated = re.search(r'Data updated till\s+(\d{1,2}-\w{3}-\d{4})', text)

    return {
        "pan":             pan.group(1).strip()  if pan     else "",
        "financial_year":  fy.group(1).strip()   if fy      else "",
        "assessment_year": ay.group(1).strip()   if ay      else "",
        "assessee_name":   name.group(1).strip() if name    else "",
        "address":         addr.group(1).strip().replace("\n", ", ") if addr else "",
        "data_updated_on": updated.group(1)      if updated else None,
    }

def parse_amount(val) -> float:
    """Safely parse numeric strings."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip())
    except:
        return 0.0

def extract_tds_tables(pdf, format_version: str) -> tuple:
    """
    Extract deductor summaries and transaction rows from TDS parts.
    Uses column-position-based extraction for reliability.
    Returns (deductors_list, transactions_list)
    """
    deductors = []

    TAN_PATTERN = re.compile(r'^[A-Z]{4}\d{5}[A-Z]$')
    SECTION_CODES = {
        "192", "192A", "193", "194", "194A", "194B", "194BA", "194C",
        "194D", "194H", "194I", "194IA", "194IB", "194IC", "194J",
        "194J(a)", "194J(b)", "194LA", "194M", "194N", "194O",
        "194Q", "194R", "194S", "195"
    }
    DATE_RE   = re.compile(r'\d{1,2}-[A-Za-z]{3}-\d{4}')
    AMOUNT_RE = re.compile(r'^\d{1,3}(,\d{3})*\.\d{2}$|^\d{4,}\.\d{2}$')

    def is_amount(val):
        clean = val.replace(",", "").strip()
        return bool(re.match(r'^\d+\.\d{2}$', clean)) and float(clean) >= 0

    def get_amounts(cells):
        return [parse_amount(c) for c in cells if is_amount(c)]

    current_deductor = None

    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if not table:
                continue
            for row in table:
                row = [str(c).strip() if c else "" for c in row]

                # ── Deductor summary row: identified by TAN pattern ──────────
                tan_idx = next((i for i, c in enumerate(row) if TAN_PATTERN.match(c)), None)
                if tan_idx is not None:
                    tan = row[tan_idx]
                    # Name = longest text cell before TAN, not a number
                    name_cells = [
                        c for c in row[:tan_idx]
                        if len(c) > 5 and not c.replace(".", "").replace(",", "").isdigit()
                    ]
                    dname = max(name_cells, key=len) if name_cells else ""

                    # Amounts are the 3 cells immediately after TAN (Amount, Tax, Deposited)
                    after_tan = [c for c in row[tan_idx + 1:] if c and c != "-"]
                    amounts = get_amounts(after_tan)

                    current_deductor = {
                        "deductor_name":        dname,
                        "tan":                  tan,
                        "total_amount_credited": amounts[0] if len(amounts) > 0 else 0,
                        "total_tax_deducted":    amounts[1] if len(amounts) > 1 else 0,
                        "total_tds_deposited":   amounts[2] if len(amounts) > 2 else 0,
                        "part_label":            "I" if format_version == "NEW" else "A",
                        "_transactions":         []
                    }
                    deductors.append(current_deductor)
                    continue

                # ── Transaction row: identified by section code ───────────────
                if current_deductor:
                    section = next((c for c in row if c in SECTION_CODES), None)
                    if section:
                        dates   = DATE_RE.findall(" ".join(row))
                        amounts = get_amounts(row)

                        # Booking status: single letter F/U/P/O/M/Z in its own cell
                        status = next(
                            (c for c in row if c in {"F", "U", "P", "O", "M", "Z"}), ""
                        )

                        # Skip glossary/reference rows — real transactions must have a date
                        if not dates:
                            continue

                        txn = {
                            "section_code":     section,
                            "transaction_date": dates[0] if len(dates) > 0 else None,
                            "booking_status":   status,
                            "date_of_booking":  dates[1] if len(dates) > 1 else None,
                            "remarks":          "",
                            "amount_paid":      amounts[0] if len(amounts) > 0 else 0,
                            "tax_deducted":     amounts[1] if len(amounts) > 1 else 0,
                            "tds_deposited":    amounts[2] if len(amounts) > 2 else 0,
                        }
                        current_deductor["_transactions"].append(txn)

    return deductors, []

def extract_self_tax(pdf, format_version: str) -> list:
    """
    Extract Part C / Part VII self-assessment / advance tax payments.
    Column order in TRACES: Sr | Major | Minor | Tax | Surcharge | Ed.Cess |
                             Penalty | Interest | Others | Total | BSR | Date | Challan | Remarks
    Strategy: anchor on major_head position, extract fixed offsets rightward.
    Only process rows that have a valid deposit date (filters out glossary rows).
    """
    results = []
    MINOR_HEADS = {"100", "102", "106", "107", "300", "400", "800", "200"}
    MAJOR_HEADS = {"0020", "0021", "0023", "0024", "0026", "0028", "0031", "0032", "0033"}
    DATE_RE     = re.compile(r'\d{1,2}-[A-Za-z]{3}-\d{4}')

    def is_decimal(val):
        clean = val.replace(",", "").strip()
        return bool(re.match(r'^\d+\.\d{2}$', clean))

    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if not table:
                continue
            for row in table:
                row = [str(c).strip() if c else "" for c in row]

                # Must have both major and minor head
                major_idx = next((i for i, c in enumerate(row) if c in MAJOR_HEADS), None)
                if major_idx is None:
                    continue
                major = row[major_idx]
                minor = next((c for c in row if c in MINOR_HEADS), None)
                if not minor:
                    continue

                # Must have a deposit date — filters out glossary/reference rows
                dates = DATE_RE.findall(" ".join(row))
                if not dates:
                    continue

                # Extract only decimal amounts (Tax, Surcharge, Ed.Cess, Penalty,
                # Interest, Others, Total) — these all end in .00
                decimal_amounts = [parse_amount(c) for c in row if is_decimal(c)]

                # BSR code: 7-digit number
                bsr = next((c for c in row if re.match(r'^\d{7}$', c.replace(",",""))), None)

                # Challan: 4-6 digit number, not BSR, not major head, not minor head
                challan = next(
                    (c for c in row if re.match(r'^\d{4,6}$', c)
                     and c != bsr
                     and c not in MAJOR_HEADS
                     and c not in MINOR_HEADS
                     and not DATE_RE.match(c)),
                    None
                )

                results.append({
                    "major_head":      major,
                    "minor_head":      minor,
                    "tax":             decimal_amounts[0] if len(decimal_amounts) > 0 else 0,
                    "surcharge":       decimal_amounts[1] if len(decimal_amounts) > 1 else 0,
                    "education_cess":  decimal_amounts[2] if len(decimal_amounts) > 2 else 0,
                    "penalty":         decimal_amounts[3] if len(decimal_amounts) > 3 else 0,
                    "interest":        decimal_amounts[4] if len(decimal_amounts) > 4 else 0,
                    "others":          decimal_amounts[5] if len(decimal_amounts) > 5 else 0,
                    "total_tax":       decimal_amounts[6] if len(decimal_amounts) > 6 else 0,
                    "bsr_code":        bsr or "",
                    "date_of_deposit": dates[0] if dates else None,
                    "challan_serial":  challan or "",
                    "remarks":         "",
                })
    return results

def parse_pdf(uploaded_file) -> dict:
    """Master parse function. Returns structured dict of all extracted data."""
    with pdfplumber.open(uploaded_file) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        fmt       = detect_format(full_text)
        header    = extract_header(full_text)
        deductors, _ = extract_tds_tables(pdf, fmt)
        self_tax  = extract_self_tax(pdf, fmt)

    return {
        "format_version": fmt,
        "header":         header,
        "deductors":      deductors,
        "self_tax":       self_tax,
    }

# ══════════════════════════════════════════════════════════════════════════════
# DB INSERT
# ══════════════════════════════════════════════════════════════════════════════

def save_to_db(client_id: int, parsed: dict) -> dict:
    h = parsed["header"]
    fmt = parsed["format_version"]

    # 1. Insert header
    header_row = {
        "pan":             h["pan"],
        "assessee_name":   h["assessee_name"],
        "address":         h["address"],
        "financial_year":  h["financial_year"],
        "assessment_year": h["assessment_year"],
        "data_updated_on": h["data_updated_on"],
        "format_version":  fmt,
        "client_id":       client_id,
    }
    hres = supabase.table("form26as_header").insert(header_row).execute()
    header_id = hres.data[0]["id"]

    deductor_count = 0
    txn_count = 0

    # 2. Insert deductors + transactions
    for d in parsed["deductors"]:
        txns = d.pop("_transactions", [])
        d["header_id"] = header_id
        dres = supabase.table("form26as_tds_deductor").insert(d).execute()
        deductor_id = dres.data[0]["id"]
        deductor_count += 1

        for t in txns:
            t["deductor_id"] = deductor_id
            supabase.table("form26as_tds_transactions").insert(t).execute()
            txn_count += 1

    # 3. Insert self tax
    for st in parsed["self_tax"]:
        st["header_id"] = header_id
        supabase.table("form26as_self_tax").insert(st).execute()

    return {
        "header_id":       header_id,
        "deductor_count":  deductor_count,
        "txn_count":       txn_count,
        "self_tax_count":  len(parsed["self_tax"]),
    }

# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div class="header-bar">
    <div>
        <h1>📊 Taxalytics</h1>
        <p>Form 26AS · Upload · Parse · Store · Analyse</p>
    </div>
</div>
""", unsafe_allow_html=True)

# ── Sidebar: Client selection ─────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 👤 Client / PAN")
    clients = fetch_clients()
    client_options = {f"{c['name']} — {c['pan']}": c for c in clients}

    mode = st.radio("", ["Select existing PAN", "Add new PAN"], label_visibility="collapsed")

    selected_client = None

    if mode == "Select existing PAN":
        if client_options:
            choice = st.selectbox("Select client", list(client_options.keys()))
            selected_client = client_options[choice]
            st.success(f"PAN: `{selected_client['pan']}`")
        else:
            st.warning("No clients yet. Add one first.")

    else:
        new_pan  = st.text_input("PAN", max_chars=10, placeholder="ABCDE1234F").upper()
        new_name = st.text_input("Full Name", placeholder="As per PAN card")
        if st.button("➕ Add Client", use_container_width=True):
            if len(new_pan) == 10 and new_name:
                try:
                    new_id = insert_client(new_pan, new_name)
                    st.success(f"Added! ID: {new_id}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Enter valid PAN (10 chars) and name.")

# ── Main: Upload + Parse ──────────────────────────────────────────────────────
col1, col2 = st.columns([1.2, 1])

with col1:
    st.markdown('<div class="card"><div class="card-title">Upload Form 26AS PDF</div>', unsafe_allow_html=True)
    uploaded = st.file_uploader("", type=["pdf"], label_visibility="collapsed")
    st.markdown('</div>', unsafe_allow_html=True)

with col2:
    st.markdown('<div class="card"><div class="card-title">Instructions</div>', unsafe_allow_html=True)
    st.markdown("""
- Download Form 26AS from **TRACES portal**
- Select or add the PAN from the sidebar
- Upload the PDF — parser handles old & new formats
- Review extracted data before saving
    """)
    st.markdown('</div>', unsafe_allow_html=True)

if uploaded and selected_client:
    with st.spinner("Parsing PDF..."):
        try:
            parsed = parse_pdf(uploaded)
        except Exception as e:
            st.error(f"Parse error: {e}")
            st.stop()

    h   = parsed["header"]
    fmt = parsed["format_version"]

    # ── Parsed header preview ─────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🔍 Extracted Data Preview")

    fcol1, fcol2, fcol3, fcol4 = st.columns(4)
    fcol1.metric("PAN",             h["pan"])
    fcol2.metric("Financial Year",  h["financial_year"])
    fcol3.metric("Assessment Year", h["assessment_year"])
    fcol4.metric("Format",          fmt)

    # PAN mismatch warning
    if h["pan"] and h["pan"] != selected_client["pan"]:
        st.warning(f"⚠️ PAN in PDF ({h['pan']}) does not match selected client ({selected_client['pan']}). Please verify.")

    # Duplicate AY check
    if ay_exists(selected_client["id"], h["assessment_year"]):
        st.error(f"❌ AY {h['assessment_year']} already exists for this PAN. Delete existing record before re-uploading.")
        st.stop()

    # ── Deductors ─────────────────────────────────────────────────────────
    if parsed["deductors"]:
        st.markdown("**TDS Deductors**")
        deductor_display = []
        for d in parsed["deductors"]:
            deductor_display.append({
                "Deductor":         d["deductor_name"],
                "TAN":              d["tan"],
                "Amount Credited":  d["total_amount_credited"],
                "Tax Deducted":     d["total_tax_deducted"],
                "TDS Deposited":    d["total_tds_deposited"],
                "Transactions":     len(d.get("_transactions", [])),
            })
        st.dataframe(pd.DataFrame(deductor_display), use_container_width=True, hide_index=True)

    # ── Self tax ──────────────────────────────────────────────────────────
    if parsed["self_tax"]:
        st.markdown("**Self Assessment / Advance Tax (Part C)**")
        st.dataframe(pd.DataFrame(parsed["self_tax"]), use_container_width=True, hide_index=True)

    if not parsed["deductors"] and not parsed["self_tax"]:
        st.info("Parser extracted header but found no TDS or tax payment rows. Verify the PDF is a standard TRACES 26AS.")

    # ── Summary counts ────────────────────────────────────────────────────
    total_tds = sum(d["total_tds_deposited"] for d in parsed["deductors"])
    total_self = sum(s["total_tax"] for s in parsed["self_tax"])

    st.markdown(f"""
    <div class="summary-row">
        <div class="summary-card">
            <div class="label">Deductors Found</div>
            <div class="value">{len(parsed["deductors"])}</div>
        </div>
        <div class="summary-card">
            <div class="label">Total TDS Deposited</div>
            <div class="value">₹{total_tds:,.0f}</div>
        </div>
        <div class="summary-card">
            <div class="label">Self / Advance Tax</div>
            <div class="value">₹{total_self:,.0f}</div>
        </div>
        <div class="summary-card">
            <div class="label">Self Tax Entries</div>
            <div class="value">{len(parsed["self_tax"])}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Save button ───────────────────────────────────────────────────────
    st.markdown("---")
    if st.button("✅ Confirm & Save to Database", type="primary", use_container_width=True):
        with st.spinner("Saving..."):
            try:
                result = save_to_db(selected_client["id"], parsed)
                st.success(f"""
                    ✅ Saved successfully!  
                    Header ID: `{result['header_id']}` | 
                    Deductors: `{result['deductor_count']}` | 
                    Transactions: `{result['txn_count']}` | 
                    Self Tax rows: `{result['self_tax_count']}`
                """)
            except Exception as e:
                st.error(f"DB Error: {e}")

elif uploaded and not selected_client:
    st.warning("Please select or add a client from the sidebar before uploading.")
