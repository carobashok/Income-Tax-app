import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
from supabase import create_client, Client

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Taxalytics", page_icon="📊", layout="wide")

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3 { font-family: 'IBM Plex Mono', monospace; }
.stApp { background-color: #FFFFFF; }

.header-bar {
    background: #1A1A2E; color: #E8E0D0;
    padding: 1.2rem 2rem; border-radius: 8px; margin-bottom: 1.5rem;
}
.header-bar h1 { color: #F5C842; margin: 0; font-size: 1.4rem; }
.header-bar p  { color: #A0A8C0; margin: 0; font-size: 0.85rem; }

.card {
    background: #FFFFFF; border: 1px solid #E0DDD5;
    border-radius: 8px; padding: 1.2rem 1.5rem; margin-bottom: 1rem;
}
.card-title {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.8rem;
    color: #888; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 0.5rem;
}
.summary-row { display: flex; gap: 1rem; margin-bottom: 1rem; }
.summary-card {
    flex: 1; background: #1A1A2E; color: #E8E0D0;
    border-radius: 8px; padding: 1rem; text-align: center;
}
.summary-card .label { font-size: 0.75rem; color: #A0A8C0; }
.summary-card .value { font-size: 1.3rem; font-weight: 600; color: #F5C842;
    font-family: 'IBM Plex Mono', monospace; }

/* Summary table styling */
.stDataFrame { border-radius: 8px; overflow: hidden; }
</style>
""", unsafe_allow_html=True)

# ── Supabase ──────────────────────────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase = get_supabase()

# ── DB helpers ────────────────────────────────────────────────────────────────
def fetch_clients():
    return supabase.table("clients").select("*").order("name").execute().data or []

def insert_client(pan: str, name: str) -> int:
    return supabase.table("clients").insert({"pan": pan.upper(), "name": name}).execute().data[0]["id"]

def ay_exists(client_id: int, assessment_year: str) -> bool:
    res = (supabase.table("form26as_header")
           .select("id").eq("client_id", client_id)
           .eq("assessment_year", assessment_year).execute())
    return len(res.data) > 0

# ── Summary query — deductor-wise ─────────────────────────────────────────────
def fetch_summary(client_id: int) -> tuple:
    """
    Returns:
      deductor_df — one row per AY per deductor
      self_tax_df — one row per AY per self-tax entry
      ay_totals   — one row per AY (for metrics)
    """
    headers = (supabase.table("form26as_header")
               .select("id, assessment_year, financial_year")
               .eq("client_id", client_id)
               .order("assessment_year")
               .execute().data or [])

    if not headers:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    ded_rows  = []
    st_rows   = []
    ay_totals = []

    for h in headers:
        hid = h["id"]
        ay  = h["assessment_year"]
        fy  = h["financial_year"]

        # ── Deductors ──────────────────────────────────────────────────────
        deductors = (supabase.table("form26as_tds_deductor")
                     .select("deductor_name, tan, total_amount_credited, total_tax_deducted, total_tds_deposited")
                     .eq("header_id", hid)
                     .execute().data or [])

        for d in deductors:
            amt = d["total_amount_credited"] or 0
            tds = d["total_tds_deposited"]   or 0
            ded_rows.append({
                "AY":                  ay,
                "FY":                  fy,
                "Deductor":            d["deductor_name"] or "",
                "TAN":                 d["tan"] or "",
                "Amount Credited (₹)": amt,
                "Tax Deducted (₹)":    d["total_tax_deducted"] or 0,
                "TDS Deposited (₹)":   tds,
                "Eff. Rate (%)":       round(tds / amt * 100, 1) if amt > 0 else 0,
            })

        # Add placeholder row for years with no deductor data
        if not deductors:
            ded_rows.append({
                "AY":                  ay,
                "FY":                  fy,
                "Deductor":            "— No TDS data —",
                "TAN":                 "",
                "Amount Credited (₹)": 0,
                "Tax Deducted (₹)":    0,
                "TDS Deposited (₹)":   0,
                "Eff. Rate (%)":       0,
            })

        # ── Self tax ───────────────────────────────────────────────────────
        self_tax = (supabase.table("form26as_self_tax")
                    .select("minor_head, major_head, total_tax, date_of_deposit, bsr_code, challan_serial")
                    .eq("header_id", hid)
                    .execute().data or [])

        MINOR_HEAD_LABELS = {
            "100": "Advance Tax",
            "300": "Self-Assessment Tax",
            "400": "Regular Assessment Tax",
            "200": "TDS/TCS",
            "800": "TDS on Property",
        }

        for s in self_tax:
            mh = s["minor_head"] or ""
            st_rows.append({
                "AY":              ay,
                "FY":              fy,
                "Type":            MINOR_HEAD_LABELS.get(mh, mh),
                "Major Head":      s["major_head"] or "",
                "Minor Head":      mh,
                "Amount (₹)":      s["total_tax"] or 0,
                "Date of Deposit": s["date_of_deposit"] or "",
                "BSR Code":        s["bsr_code"] or "",
                "Challan No":      s["challan_serial"] or "",
            })

        # ── AY totals (for metrics row) ────────────────────────────────────
        gross  = sum(d["total_amount_credited"] or 0 for d in deductors)
        tds    = sum(d["total_tds_deposited"]   or 0 for d in deductors)
        adv    = sum(s["total_tax"] or 0 for s in self_tax if s["minor_head"] == "100")
        self_  = sum(s["total_tax"] or 0 for s in self_tax if s["minor_head"] == "300")
        reg    = sum(s["total_tax"] or 0 for s in self_tax if s["minor_head"] == "400")
        ay_totals.append({
            "AY":               ay,
            "FY":               fy,
            "Gross Income":     gross,
            "TDS":              tds,
            "Advance Tax":      adv,
            "Self-Assess Tax":  self_,
            "Regular Tax":      reg,
            "Total Tax":        tds + adv + self_ + reg,
        })

    return (
        pd.DataFrame(ded_rows),
        pd.DataFrame(st_rows),
        pd.DataFrame(ay_totals),
    )

# ══════════════════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════════════════

def detect_format(text: str) -> str:
    if "PART-I" in text or "PART I" in text or "Part-I" in text:
        return "NEW"
    if "PART A" in text or "Part A" in text:
        return "OLD"
    return "NEW"

def extract_header(text: str) -> dict:
    pan     = re.search(r'PAN[)\s]*([A-Z]{5}[0-9]{4}[A-Z])', text)
    fy      = re.search(r'Financial Year[:\s]+(\d{4}-\d{2,4})', text)
    ay      = re.search(r'Assessment Year[:\s]+(\d{4}-\d{2,4})', text)
    name    = re.search(r'Name of Assessee\s+([A-Z ]+)', text)
    addr    = re.search(r'Address of Assessee\s+(.+?)(?=Above data|$)', text, re.DOTALL)
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
    if val is None: return 0.0
    try: return float(str(val).replace(",", "").strip())
    except: return 0.0

def extract_tds_tables(pdf, format_version: str) -> list:
    deductors = []
    TAN_PATTERN  = re.compile(r'^[A-Z]{4}\d{5}[A-Z]$')
    SECTION_CODES = {
        "192","192A","193","194","194A","194B","194BA","194C",
        "194D","194H","194I","194IA","194IB","194IC","194J",
        "194J(a)","194J(b)","194LA","194M","194N","194O",
        "194Q","194R","194S","195"
    }
    DATE_RE = re.compile(r'\d{1,2}-[A-Za-z]{3}-\d{4}')

    def is_amount(val):
        clean = val.replace(",", "").strip()
        try: return bool(re.match(r'^\d+\.\d{2}$', clean)) and float(clean) >= 0
        except: return False

    def get_amounts(cells):
        return [parse_amount(c) for c in cells if is_amount(c)]

    current_deductor = None

    for page in pdf.pages:
        for table in (page.extract_tables() or []):
            for row in table:
                row = [str(c).strip() if c else "" for c in row]

                # Deductor summary row
                tan_idx = next((i for i, c in enumerate(row) if TAN_PATTERN.match(c)), None)
                if tan_idx is not None:
                    tan = row[tan_idx]
                    name_cells = [c for c in row[:tan_idx]
                                  if len(c) > 5 and not c.replace(".", "").replace(",", "").isdigit()]
                    dname   = max(name_cells, key=len) if name_cells else ""
                    amounts = get_amounts([c for c in row[tan_idx+1:] if c and c != "-"])
                    current_deductor = {
                        "deductor_name":         dname,
                        "tan":                   tan,
                        "total_amount_credited":  amounts[0] if len(amounts) > 0 else 0,
                        "total_tax_deducted":     amounts[1] if len(amounts) > 1 else 0,
                        "total_tds_deposited":    amounts[2] if len(amounts) > 2 else 0,
                        "part_label":             "I" if format_version == "NEW" else "A",
                        "_transactions":          []
                    }
                    deductors.append(current_deductor)
                    continue

                # Transaction row
                if current_deductor:
                    section = next((c for c in row if c in SECTION_CODES), None)
                    if section:
                        dates  = DATE_RE.findall(" ".join(row))
                        if not dates: continue   # skip glossary rows
                        amounts = get_amounts(row)
                        status  = next((c for c in row if c in {"F","U","P","O","M","Z"}), "")
                        current_deductor["_transactions"].append({
                            "section_code":     section,
                            "transaction_date": dates[0],
                            "booking_status":   status,
                            "date_of_booking":  dates[1] if len(dates) > 1 else None,
                            "remarks":          "",
                            "amount_paid":      amounts[0] if len(amounts) > 0 else 0,
                            "tax_deducted":     amounts[1] if len(amounts) > 1 else 0,
                            "tds_deposited":    amounts[2] if len(amounts) > 2 else 0,
                        })
    return deductors

def extract_self_tax(pdf, format_version: str) -> list:
    results = []
    MINOR_HEADS = {"100","102","106","107","300","400","800","200"}
    MAJOR_HEADS = {"0020","0021","0023","0024","0026","0028","0031","0032","0033"}
    DATE_RE = re.compile(r'\d{1,2}-[A-Za-z]{3}-\d{4}')

    def is_decimal(val):
        clean = val.replace(",", "").strip()
        return bool(re.match(r'^\d+\.\d{2}$', clean))

    for page in pdf.pages:
        for table in (page.extract_tables() or []):
            for row in table:
                row = [str(c).strip() if c else "" for c in row]
                major_idx = next((i for i, c in enumerate(row) if c in MAJOR_HEADS), None)
                if major_idx is None: continue
                major = row[major_idx]
                minor = next((c for c in row if c in MINOR_HEADS), None)
                if not minor: continue
                dates = DATE_RE.findall(" ".join(row))
                if not dates: continue   # skip glossary rows
                decimals = [parse_amount(c) for c in row if is_decimal(c)]
                bsr     = next((c for c in row if re.match(r'^\d{7}$', c.replace(",", ""))), None)
                challan = next(
                    (c for c in row if re.match(r'^\d{4,6}$', c)
                     and c != bsr and c not in MAJOR_HEADS
                     and c not in MINOR_HEADS and not DATE_RE.match(c)), None)
                results.append({
                    "major_head":      major,
                    "minor_head":      minor,
                    "tax":             decimals[0] if len(decimals) > 0 else 0,
                    "surcharge":       decimals[1] if len(decimals) > 1 else 0,
                    "education_cess":  decimals[2] if len(decimals) > 2 else 0,
                    "penalty":         decimals[3] if len(decimals) > 3 else 0,
                    "interest":        decimals[4] if len(decimals) > 4 else 0,
                    "others":          decimals[5] if len(decimals) > 5 else 0,
                    "total_tax":       decimals[6] if len(decimals) > 6 else 0,
                    "bsr_code":        bsr or "",
                    "date_of_deposit": dates[0],
                    "challan_serial":  challan or "",
                    "remarks":         "",
                })
    return results

def parse_pdf(uploaded_file) -> dict:
    with pdfplumber.open(uploaded_file) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        fmt       = detect_format(full_text)
        header    = extract_header(full_text)
        deductors = extract_tds_tables(pdf, fmt)
        self_tax  = extract_self_tax(pdf, fmt)
    return {"format_version": fmt, "header": header,
            "deductors": deductors, "self_tax": self_tax}

# ── DB insert ─────────────────────────────────────────────────────────────────
def save_to_db(client_id: int, parsed: dict) -> dict:
    h   = parsed["header"]
    fmt = parsed["format_version"]
    hres = supabase.table("form26as_header").insert({
        "pan": h["pan"], "assessee_name": h["assessee_name"],
        "address": h["address"], "financial_year": h["financial_year"],
        "assessment_year": h["assessment_year"],
        "data_updated_on": h["data_updated_on"],
        "format_version": fmt, "client_id": client_id,
    }).execute()
    header_id = hres.data[0]["id"]

    deductor_count = txn_count = 0
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
    for st_row in parsed["self_tax"]:
        st_row["header_id"] = header_id
        supabase.table("form26as_self_tax").insert(st_row).execute()

    return {"header_id": header_id, "deductor_count": deductor_count,
            "txn_count": txn_count, "self_tax_count": len(parsed["self_tax"])}

# ══════════════════════════════════════════════════════════════════════════════
# AIS PARSER
# ══════════════════════════════════════════════════════════════════════════════

def convert_ais_date(date_str: str) -> str:
    """Convert DD/MM/YYYY to YYYY-MM-DD for PostgreSQL."""
    if not date_str:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
    except:
        return date_str
    """Extract PAN, name, financial year from AIS PDF text."""
    pan  = re.search(r'\b([A-Z]{5}[0-9]{4}[A-Z])\b', text)
    fy   = re.search(r'Financial Year\s+(20\d{2}-\d{2,4})', text)
    name = re.search(r'Name of Assessee\s*\n?\s*([A-Z][A-Z ]+)', text)

    raw_fy = fy.group(1).strip() if fy else ""
    ay = ""
    if raw_fy:
        try:
            start_yr = int(raw_fy.split("-")[0])
            ay = f"{start_yr + 1}-{str(start_yr + 2)[-2:]}"
        except: pass

    assessee = ""
    if name:
        assessee = name.group(1).strip()
    else:
        # fallback — look for name after PAN line
        m = re.search(r'AEUPA\w+\s+XXXX.+?\s+([A-Z][A-Z ]{5,})', text)
        if m:
            assessee = m.group(1).strip()

    return {
        "pan":             pan.group(1).strip() if pan else "",
        "financial_year":  raw_fy,
        "assessment_year": ay,
        "assessee_name":   assessee,
        "address":         "",
        "data_updated_on": None,
    }

def extract_ais_header(text: str) -> dict:
    """Extract PAN, name, financial year from AIS PDF text."""
    pan  = re.search(r'\b([A-Z]{5}[0-9]{4}[A-Z])\b', text)
    fy   = re.search(r'Financial Year\s+(20\d{2}-\d{2,4})', text)
    name = re.search(r'Name of Assessee\s*\n?\s*([A-Z][A-Z ]+)', text)

    raw_fy = fy.group(1).strip() if fy else ""
    ay = ""
    if raw_fy:
        try:
            start_yr = int(raw_fy.split("-")[0])
            ay = f"{start_yr + 1}-{str(start_yr + 2)[-2:]}"
        except: pass

    assessee = ""
    if name:
        assessee = name.group(1).strip()
    else:
        m = re.search(r'[A-Z]{5}[0-9]{4}[A-Z]\s+XXXX.+?\s+([A-Z][A-Z ]{5,})', text)
        if m:
            assessee = m.group(1).strip()

    return {
        "pan":             pan.group(1).strip() if pan else "",
        "financial_year":  raw_fy,
        "assessment_year": ay,
        "assessee_name":   assessee,
        "address":         "",
        "data_updated_on": None,
    }

def extract_ais_tax_payments(pdf) -> list:
    """
    Extract Part B3 tax payments from AIS PDF.
    Uses both table extraction and raw text parsing as fallback.
    """
    results = []

    MAJOR_HEAD_MAP = {
        "income tax (other than companies)": "0021",
        "income tax other than companies":   "0021",
        "corporation tax":                   "0020",
        "income tax":                        "0021",
    }
    MINOR_HEAD_MAP = {
        "advance tax":                        "100",
        "self assessment":                    "300",
        "self assessment tax":                "300",
        "regular assessment":                 "400",
        "regular assessment tax":             "400",
        "outstanding demand":                 "400",
        "tds/tcs regular assessment":         "400",
        "outstanding demand (regular assessment tax)": "400",
        "tds/tcs":                            "200",
        "surtax":                             "102",
    }

    DATE_RE = re.compile(r'\d{2}/\d{2}/\d{4}')
    FY_RE   = re.compile(r'20\d{2}-\d{2,4}')

    def find_major(text):
        t = text.lower()
        for label, code in MAJOR_HEAD_MAP.items():
            if label in t:
                return code
        return None

    def find_minor(text):
        t = text.lower()
        for label, code in MINOR_HEAD_MAP.items():
            if label in t:
                return code
        return None

    def extract_amounts(text):
        """Extract all Indian-format integers from text, skip small nos and years."""
        nums = []
        for m in re.finditer(r'[\d,]+', text):
            clean = m.group().replace(",", "")
            if re.match(r'^\d+$', clean):
                val = int(clean)
                if val == 0:
                    nums.append(0.0)
                elif val >= 100 and not (2000 <= val <= 2100):
                    nums.append(float(val))
        return nums

    # ── Strategy 1: Table extraction ─────────────────────────────────────
    for page in pdf.pages:
        for table in (page.extract_tables() or []):
            for row in table:
                # Replace newlines inside cells — AIS wraps cell text with \n
                row_cells = [str(c).replace("\n", " ").strip() if c else "" for c in row]
                row_text  = " ".join(row_cells)
                fys = FY_RE.findall(row_text)
                if not fys:
                    continue

                # Must match a major head
                major = find_major(row_text)
                if not major:
                    continue

                # Must match a minor head
                minor = find_minor(row_text)
                if not minor:
                    continue

                # Date may or may not be in this row (wide tables get cut)
                dates = DATE_RE.findall(row_text)

                bsr = next((c.replace(",","") for c in row_cells
                            if re.match(r'^\d{7}$', c.replace(",",""))), None)

                # AIS B3 column order (0-indexed):
                # 0=SR, 1=FY, 2=MAJOR HEAD, 3=MINOR HEAD, 4=TAX(A),
                # 5=SURCHARGE(B), 6=ED CESS(C), 7=OTHERS(D),
                # 8=TOTAL(A+B+C+D), 9=BSR CODE, 10=DATE, 11=CHALLAN SERIAL, 12=CIN
                def clean_num(val):
                    try:
                        return float(val.replace(",","").strip())
                    except:
                        return 0.0

                n = len(row_cells)
                tax      = clean_num(row_cells[4])  if n > 4  else 0
                surch    = clean_num(row_cells[5])  if n > 5  else 0
                edcess   = clean_num(row_cells[6])  if n > 6  else 0
                others   = clean_num(row_cells[7])  if n > 7  else 0
                total    = clean_num(row_cells[8])  if n > 8  else 0
                bsr      = row_cells[9].replace(",","")  if n > 9  else ""
                date_raw = row_cells[10]             if n > 10 else ""
                challan  = row_cells[11]             if n > 11 else ""

                # Fallback date from any DD/MM/YYYY in row if column 10 is empty
                if not DATE_RE.match(date_raw):
                    dates = DATE_RE.findall(row_text)
                    date_raw = dates[0] if dates else ""

                results.append({
                    "major_head":      major,
                    "minor_head":      minor,
                    "tax":             tax,
                    "surcharge":       surch,
                    "education_cess":  edcess,
                    "others":          others,
                    "total_tax":       total,
                    "penalty":         0,
                    "interest":        0,
                    "bsr_code":        bsr,
                    "date_of_deposit": convert_ais_date(date_raw) if date_raw else None,
                    "challan_serial":  challan,
                    "remarks":         "",
                    "source":          "AIS",
                })

    if results:
        return results

    # ── Strategy 2: Raw text fallback (when table extraction misses rows) ─
    # Look for lines containing a date AND a FY AND a major/minor head text
    full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    in_b3 = False
    for line in full_text.split("\n"):
        if "Part B3" in line or "B3-Information relating to payment" in line:
            in_b3 = True
        if in_b3 and ("Part B4" in line or "B4-Information" in line):
            in_b3 = False
        if not in_b3:
            continue

        dates = DATE_RE.findall(line)
        fys   = FY_RE.findall(line)
        if not dates or not fys:
            continue

        major = find_major(line)
        minor = find_minor(line)
        if not major or not minor:
            continue

        bsr     = next((m.group() for m in re.finditer(r'\b\d{7}\b', line)), None)
        amounts = extract_amounts(line)
        exclude = {float(bsr)} if bsr else set()
        amounts = [a for a in amounts if a not in exclude]

        results.append({
            "major_head":      major,
            "minor_head":      minor,
            "tax":             amounts[0] if len(amounts) > 0 else 0,
            "surcharge":       amounts[1] if len(amounts) > 1 else 0,
            "education_cess":  amounts[2] if len(amounts) > 2 else 0,
            "others":          amounts[3] if len(amounts) > 3 else 0,
            "total_tax":       amounts[4] if len(amounts) > 4 else 0,
            "penalty":         0,
            "interest":        0,
            "bsr_code":        bsr or "",
            "date_of_deposit": convert_ais_date(dates[0]),
            "challan_serial":  "",
            "remarks":         "",
            "source":          "AIS",
        })

def parse_ais_pdf(uploaded_file, password: str = "") -> dict:
    """Parse AIS PDF — extract header and Part B3 tax payments only."""
    import pikepdf, io
    # Try to unlock if password protected
    try:
        pdf_bytes = uploaded_file.read()
        try:
            # First try without password
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                full_text    = "\n".join(p.extract_text() or "" for p in pdf.pages)
                header       = extract_ais_header(full_text)
                tax_payments = extract_ais_tax_payments(pdf)
        except Exception:
            # Try with password using pikepdf to decrypt first
            if not password:
                raise ValueError("PDF is password protected. Please enter the password.")
            unlocked = io.BytesIO()
            with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf_locked:
                pdf_locked.save(unlocked)
            unlocked.seek(0)
            with pdfplumber.open(unlocked) as pdf:
                full_text    = "\n".join(p.extract_text() or "" for p in pdf.pages)
                header       = extract_ais_header(full_text)
                tax_payments = extract_ais_tax_payments(pdf)
    except ValueError:
        raise
    except Exception as e:
        raise Exception(f"Could not open PDF: {e}")

    return {
        "source":       "AIS",
        "header":       header,
        "tax_payments": tax_payments,
    }

def save_ais_to_db(client_id: int, parsed: dict, header_id: int) -> int:
    """Insert AIS tax payments into form26as_self_tax under existing header."""
    count = 0
    for row in parsed["tax_payments"]:
        row["header_id"] = header_id
        supabase.table("form26as_self_tax").insert(row).execute()
        count += 1
    return count

def get_header_id(client_id: int, assessment_year: str):
    """Fetch existing header_id for a given client + AY."""
    res = (supabase.table("form26as_header")
           .select("id")
           .eq("client_id", client_id)
           .eq("assessment_year", assessment_year)
           .execute())
    return res.data[0]["id"] if res.data else None


st.markdown("""
<div class="header-bar">
    <div>
        <h1>📊 Taxalytics</h1>
        <p>Form 26AS · Upload · Parse · Store · Analyse</p>
    </div>
</div>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 👤 Client / PAN")
    clients        = fetch_clients()
    client_options = {f"{c['name']} — {c['pan']}": c for c in clients}
    mode           = st.radio("", ["Select existing PAN", "Add new PAN"],
                              label_visibility="collapsed")
    selected_client = None

    if mode == "Select existing PAN":
        if client_options:
            choice          = st.selectbox("Select client", list(client_options.keys()))
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
                    insert_client(new_pan, new_name)
                    st.success("Added!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Enter valid PAN (10 chars) and name.")

    st.markdown("---")
    st.markdown("### Navigation")
    page = st.radio("", ["Upload 26AS", "Upload AIS", "Year-wise Summary"],
                    label_visibility="collapsed")

# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — UPLOAD
# ══════════════════════════════════════════════════════════════════════════════
if page == "Upload 26AS":

    col1, col2 = st.columns([1.2, 1])
    with col1:
        st.markdown('<div class="card"><div class="card-title">Upload Form 26AS PDF</div>',
                    unsafe_allow_html=True)
        uploaded = st.file_uploader("", type=["pdf"], label_visibility="collapsed")
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div class="card"><div class="card-title">Instructions</div>',
                    unsafe_allow_html=True)
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

        st.markdown("#### Extracted Data Preview")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PAN",             h["pan"])
        c2.metric("Financial Year",  h["financial_year"])
        c3.metric("Assessment Year", h["assessment_year"])
        c4.metric("Format",          fmt)

        if h["pan"] and h["pan"] != selected_client["pan"]:
            st.warning(f"⚠️ PAN in PDF ({h['pan']}) ≠ selected client ({selected_client['pan']}). Please verify.")

        if ay_exists(selected_client["id"], h["assessment_year"]):
            st.error(f"❌ AY {h['assessment_year']} already exists for this PAN. Delete before re-uploading.")
            st.stop()

        if parsed["deductors"]:
            st.markdown("**TDS Deductors**")
            st.dataframe(pd.DataFrame([{
                "Deductor":        d["deductor_name"],
                "TAN":             d["tan"],
                "Amount Credited": d["total_amount_credited"],
                "Tax Deducted":    d["total_tax_deducted"],
                "TDS Deposited":   d["total_tds_deposited"],
                "Transactions":    len(d.get("_transactions", [])),
            } for d in parsed["deductors"]]), use_container_width=True, hide_index=True)

        if parsed["self_tax"]:
            st.markdown("**Self Assessment / Advance Tax (Part C)**")
            st.dataframe(pd.DataFrame(parsed["self_tax"]),
                         use_container_width=True, hide_index=True)

        if not parsed["deductors"] and not parsed["self_tax"]:
            st.info("ℹ️ No TDS or tax payment data found in this PDF — header will still be saved. Summary will show ₹0 for this year.")

        total_tds  = sum(d["total_tds_deposited"] for d in parsed["deductors"])
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

        st.markdown("---")
        if st.button("✅ Confirm & Save to Database", type="primary", use_container_width=True):
            with st.spinner("Saving..."):
                try:
                    result = save_to_db(selected_client["id"], parsed)
                    st.success(
                        f"✅ Saved! Header ID: `{result['header_id']}` | "
                        f"Deductors: `{result['deductor_count']}` | "
                        f"Transactions: `{result['txn_count']}` | "
                        f"Self Tax rows: `{result['self_tax_count']}`"
                    )
                except Exception as e:
                    st.error(f"DB Error: {e}")

    elif uploaded and not selected_client:
        st.warning("Please select or add a client from the sidebar before uploading.")

# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — UPLOAD AIS
# ══════════════════════════════════════════════════════════════════════════════
elif page == "Upload AIS":

    col1, col2 = st.columns([1.2, 1])
    with col1:
        st.markdown('<div class="card"><div class="card-title">Upload AIS PDF</div>',
                    unsafe_allow_html=True)
        uploaded_ais = st.file_uploader("", type=["pdf"], label_visibility="collapsed",
                                        key="ais_uploader")
        ais_password = st.text_input("PDF Password (if protected)",
                                     type="password",
                                     placeholder="e.g. panDDMMYYYY",
                                     help="AIS password is usually PAN in lowercase + date of birth (DDMMYYYY)")
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div class="card"><div class="card-title">Instructions</div>',
                    unsafe_allow_html=True)
        st.markdown("""
- Download AIS from **Income Tax e-filing portal**
- Remove PDF password before uploading *(File → Print → Save as PDF)*
- AIS captures **tax payments (Part B3)** for FY 2023-24 onwards
- The corresponding 26AS must already be uploaded for the same AY
        """)
        st.markdown('</div>', unsafe_allow_html=True)

    if uploaded_ais and selected_client:
        with st.spinner("Parsing AIS PDF..."):
            try:
                parsed_ais = parse_ais_pdf(uploaded_ais, password=ais_password)
            except ValueError as e:
                st.error(f"🔒 {e}")
                st.stop()
            except Exception as e:
                st.error(f"Parse error: {e}")
                st.stop()

        h  = parsed_ais["header"]
        ay = h["assessment_year"]
        fy = h["financial_year"]

        st.markdown("---")
        st.markdown("#### Extracted AIS Data")

        c1, c2, c3 = st.columns(3)
        c1.metric("PAN",             h["pan"] or selected_client["pan"])
        c2.metric("Financial Year",  fy)
        c3.metric("Assessment Year", ay)

        # Check if 26AS header exists for this AY
        header_id = get_header_id(selected_client["id"], ay)

        if not header_id:
            st.warning(
                f"⚠️ No 26AS uploaded yet for AY {ay}. "
                f"Please upload the 26AS for this year first, then come back to upload AIS."
            )
            st.stop()

        # Check if AIS already uploaded for this AY
        existing_ais = (supabase.table("form26as_self_tax")
                        .select("id")
                        .eq("header_id", header_id)
                        .eq("source", "AIS")
                        .execute().data or [])
        if existing_ais:
            st.error(f"❌ AIS data already exists for AY {ay}. "
                     f"Delete existing AIS entries before re-uploading.")
            st.stop()

        # Preview tax payments
        if parsed_ais["tax_payments"]:
            st.markdown("**Part B3 — Tax Payments**")

            MINOR_LABELS = {
                "100": "Advance Tax", "300": "Self-Assessment Tax",
                "400": "Regular Assessment Tax", "200": "TDS/TCS",
            }
            preview = []
            for r in parsed_ais["tax_payments"]:
                preview.append({
                    "Type":            MINOR_LABELS.get(r["minor_head"], r["minor_head"]),
                    "Major Head":      r["major_head"],
                    "Tax (₹)":         r["tax"],
                    "Total Tax (₹)":   r["total_tax"],
                    "Date of Deposit": r["date_of_deposit"],
                    "BSR Code":        r["bsr_code"],
                    "Challan No":      r["challan_serial"],
                })
            st.dataframe(pd.DataFrame(preview), use_container_width=True, hide_index=True)

            total_tax = sum(r["total_tax"] for r in parsed_ais["tax_payments"])
            st.markdown(f"""
            <div class="summary-row">
                <div class="summary-card">
                    <div class="label">Tax Payment Entries</div>
                    <div class="value">{len(parsed_ais["tax_payments"])}</div>
                </div>
                <div class="summary-card">
                    <div class="label">Total Tax Paid</div>
                    <div class="value">₹{total_tax:,.0f}</div>
                </div>
                <div class="summary-card">
                    <div class="label">Linked to AY</div>
                    <div class="value">{ay}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.info("No tax payment entries found in Part B3 of this AIS. "
                    "This is normal for years where advance/self-assessment tax was nil.")

        st.markdown("---")
        if st.button("✅ Confirm & Save AIS Data", type="primary", use_container_width=True):
            with st.spinner("Saving..."):
                try:
                    count = save_ais_to_db(selected_client["id"], parsed_ais, header_id)
                    st.success(
                        f"✅ AIS data saved! "
                        f"{count} tax payment entries linked to AY {ay}."
                    )
                except Exception as e:
                    st.error(f"DB Error: {e}")

    elif uploaded_ais and not selected_client:
        st.warning("Please select a client from the sidebar before uploading.")


elif page == "Year-wise Summary":

    st.markdown("#### 📋 Year-wise Tax Summary")

    if not selected_client:
        st.warning("Please select a client from the sidebar.")
        st.stop()

    with st.spinner("Loading data..."):
        df_ded, df_st, df_totals = fetch_summary(selected_client["id"])

    if df_ded.empty and df_st.empty:
        st.info(f"No data uploaded yet for {selected_client['name']}. "
                f"Go to Upload page and add Form 26AS PDFs first.")
        st.stop()

    st.markdown(f"**{selected_client['name']}** &nbsp;|&nbsp; PAN: `{selected_client['pan']}`")

    # ── Quick metrics ──────────────────────────────────────────────────────
    if not df_totals.empty:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Years of Data",   len(df_totals))
        m2.metric("Total Income",    f"₹{df_totals['Gross Income'].sum():,.0f}")
        m3.metric("Total TDS",       f"₹{df_totals['TDS'].sum():,.0f}")
        m4.metric("Total Tax Paid",  f"₹{df_totals['Total Tax'].sum():,.0f}")

    st.markdown("---")

    # ── Tabs ──────────────────────────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs(["Deductor-wise", "Self/Advance Tax", "AY Totals"])

    def fmt_inr(val):
        try:
            return "—" if val == 0 else f"₹{val:,.0f}"
        except: return val

    # ── Tab 1: Deductor-wise ──────────────────────────────────────────────
    with tab1:
        if df_ded.empty:
            st.info("No deductor data found.")
        else:
            # Totals row
            totals_ded = pd.DataFrame([{
                "AY":                  "TOTAL",
                "FY":                  "",
                "Deductor":            f"{len(df_ded)} entries",
                "TAN":                 "",
                "Amount Credited (₹)": df_ded["Amount Credited (₹)"].sum(),
                "Tax Deducted (₹)":    df_ded["Tax Deducted (₹)"].sum(),
                "TDS Deposited (₹)":   df_ded["TDS Deposited (₹)"].sum(),
                "Eff. Rate (%)":       round(
                    df_ded["TDS Deposited (₹)"].sum() /
                    df_ded["Amount Credited (₹)"].sum() * 100, 1
                ) if df_ded["Amount Credited (₹)"].sum() > 0 else 0,
            }])
            df_ded_fmt = pd.concat([df_ded, totals_ded], ignore_index=True)

            for col in ["Amount Credited (₹)", "Tax Deducted (₹)", "TDS Deposited (₹)"]:
                df_ded_fmt[col] = df_ded_fmt[col].apply(fmt_inr)
            df_ded_fmt["Eff. Rate (%)"] = df_ded_fmt["Eff. Rate (%)"].apply(
                lambda x: f"{x}%" if x else "—"
            )

            st.dataframe(
                df_ded_fmt,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "AY":                  st.column_config.TextColumn("AY",           width=90),
                    "FY":                  st.column_config.TextColumn("FY",           width=90),
                    "Deductor":            st.column_config.TextColumn("Deductor",     width=300),
                    "TAN":                 st.column_config.TextColumn("TAN",          width=110),
                    "Amount Credited (₹)": st.column_config.TextColumn("Amt Credited", width=120),
                    "Tax Deducted (₹)":    st.column_config.TextColumn("Tax Deducted", width=120),
                    "TDS Deposited (₹)":   st.column_config.TextColumn("TDS Deposited",width=120),
                    "Eff. Rate (%)":       st.column_config.TextColumn("Eff. Rate",    width=90),
                }
            )

    # ── Tab 2: Self / Advance Tax ─────────────────────────────────────────
    with tab2:
        if df_st.empty:
            st.info("No self/advance tax entries found.")
        else:
            totals_st = pd.DataFrame([{
                "AY":          "TOTAL",
                "FY":          "",
                "Type":        f"{len(df_st)} entries",
                "Major Head":  "",
                "Minor Head":  "",
                "Amount (₹)":  df_st["Amount (₹)"].sum(),
                "Date of Deposit": "",
                "BSR Code":    "",
                "Challan No":  "",
            }])
            df_st_fmt = pd.concat([df_st, totals_st], ignore_index=True)
            df_st_fmt["Amount (₹)"] = df_st_fmt["Amount (₹)"].apply(fmt_inr)

            st.dataframe(
                df_st_fmt,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "AY":              st.column_config.TextColumn("Asst. Year", width="small"),
                    "FY":              st.column_config.TextColumn("Fin. Year",  width="small"),
                    "Type":            st.column_config.TextColumn("Tax Type",   width="medium"),
                    "Major Head":      st.column_config.TextColumn("Major Head", width="small"),
                    "Minor Head":      st.column_config.TextColumn("Minor Head", width="small"),
                    "Amount (₹)":      st.column_config.TextColumn("Amount",     width="medium"),
                    "Date of Deposit": st.column_config.TextColumn("Date",       width="medium"),
                    "BSR Code":        st.column_config.TextColumn("BSR Code",   width="medium"),
                    "Challan No":      st.column_config.TextColumn("Challan No", width="medium"),
                }
            )

    # ── Tab 3: AY Totals ──────────────────────────────────────────────────
    with tab3:
        if df_totals.empty:
            st.info("No data found.")
        else:
            totals_ay = pd.DataFrame([{
                "AY":              "TOTAL",
                "FY":              "",
                "Gross Income":    df_totals["Gross Income"].sum(),
                "TDS":             df_totals["TDS"].sum(),
                "Advance Tax":     df_totals["Advance Tax"].sum(),
                "Self-Assess Tax": df_totals["Self-Assess Tax"].sum(),
                "Regular Tax":     df_totals["Regular Tax"].sum(),
                "Total Tax":       df_totals["Total Tax"].sum(),
            }])
            df_ay_fmt = pd.concat([df_totals, totals_ay], ignore_index=True)

            for col in ["Gross Income","TDS","Advance Tax","Self-Assess Tax","Regular Tax","Total Tax"]:
                df_ay_fmt[col] = df_ay_fmt[col].apply(fmt_inr)

            st.dataframe(
                df_ay_fmt,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "AY":              st.column_config.TextColumn("Asst. Year",     width="small"),
                    "FY":              st.column_config.TextColumn("Fin. Year",       width="small"),
                    "Gross Income":    st.column_config.TextColumn("Gross Income",    width="medium"),
                    "TDS":             st.column_config.TextColumn("TDS Deposited",   width="medium"),
                    "Advance Tax":     st.column_config.TextColumn("Advance Tax",     width="medium"),
                    "Self-Assess Tax": st.column_config.TextColumn("Self-Assess Tax", width="medium"),
                    "Regular Tax":     st.column_config.TextColumn("Regular Tax",     width="medium"),
                    "Total Tax":       st.column_config.TextColumn("Total Tax Paid",  width="medium"),
                }
            )

    # ── Export to Excel — all sheets ──────────────────────────────────────
    st.markdown("---")
    def to_excel_multi() -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            if not df_ded.empty:
                df_ded.to_excel(writer, index=False, sheet_name="Deductor-wise")
            if not df_st.empty:
                df_st.to_excel(writer, index=False, sheet_name="Self-Advance Tax")
            if not df_totals.empty:
                df_totals.to_excel(writer, index=False, sheet_name="AY Totals")
        return buf.getvalue()

    st.download_button(
        label="⬇️ Download as Excel (all sheets)",
        data=to_excel_multi(),
        file_name=f"Taxalytics_{selected_client['pan']}_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
