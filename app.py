import streamlit as st
import re
import logging
import concurrent.futures
import time
import html
import json
from typing import List, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from snowflake.snowpark import Session
from snowflake.core import Root
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from io import BytesIO
from cachetools import LRUCache

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# CACHE & MESSAGE MANAGEMENT HELPERS
def cache_with_limit(cache_dict, key, value):
    cache_dict[key] = value

def get_recent_conversation_context(messages, max_pairs=2):
    """
    Get only the last N user/assistant pairs for conversation context.
    Prioritize follow-up questions.
    """
    history = []
    for msg in messages[-(max_pairs * 2):]:
        role = "User" if msg["role"] == "user" else "Assistant"
        weight = 1.5 if msg["content"].lower().startswith(("why", "how", "what")) else 1.0
        history.append((f"{role}: {msg['content']}", weight))
    history.sort(key=lambda x: x[1], reverse=True)
    return "\n".join(h[0] for h in history) if history else ""

# SNOWFLAKE CONNECTION
@st.cache_resource
def create_snowflake_session():
    try:
        connection_parameters = {
            "account": st.secrets["account"],
            "user": st.secrets["user"],
            "password": st.secrets["password"],
            "warehouse": st.secrets["warehouse"],
            "database": st.secrets["database"],
            "schema": st.secrets["schema"],
            "role": st.secrets["role"],
        }
    except (KeyError, TypeError) as e:
        logging.error(f"Failed to load secrets from Streamlit: {e}")
        st.error("Failed to load Snowflake credentials from secrets. Please check your Streamlit Cloud secrets configuration.")
        st.stop()

    try:
        session = Session.builder.configs(connection_parameters).create()
        logging.info("Snowflake session created successfully")
        return session
    except Exception as e:
        logging.error(f"Failed to create Snowflake session: {e}")
        st.error(f"Cannot connect to Snowflake: {e}. Please check credentials and try again.")
        raise

try:
    session = create_snowflake_session()
    root = Root(session)
    search_service = (
        root.databases["CORTEX_SEARCH_TUTORIAL_DB"]
        .schemas["PUBLIC"]
        .cortex_search_services["FOMC_SEARCH_SERVICE"]
    )
except Exception as e:
    st.error("Failed to initialize Snowflake connection. Please check logs and secrets configuration.")
    st.stop()

# TEXT & FILE HELPERS
def extract_target_years(query: str) -> List[int]:
    return [int(y) for y in re.findall(r"20\d{2}", query)]

def extract_file_year(file_name: str) -> int:
    match = re.search(r"(\d{4})", file_name)
    return int(match.group(1)) if match else 0

def clean_chunk(chunk: str) -> str:
    cleaned = re.sub(r"!\[.*?\]\(.*?\)", "", chunk)
    cleaned = re.sub(r"#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def extract_clean_title(file_name: str) -> str:
    month_map = {
        "01": "January", "02": "February", "03": "March", "04": "April",
        "05": "May", "06": "June", "07": "July", "08": "August",
        "09": "September", "10": "October", "11": "November", "12": "December",
    }
    match = re.search(r"(\d{4})(\d{2})(\d{2})", file_name)
    if match:
        year, month, day = match.groups()
        date_str = f"{month_map.get(month, month)} {int(day)}, {year}"
    else:
        date_str = "Unknown Date"
    fname = file_name.lower()
    if "beigebook" in fname or "beigebook_" in fname:
        doc_type = "Beige Book"
    elif "longerungoals" in fname or "fomc_longerungoals" in fname:
        doc_type = "FOMC Longer-Run Goals"
    elif "presconf" in fname or "fomcpresconf" in fname:
        doc_type = "Press Conference"
    elif "projtabl" in fname or "fomcprojtabl" in fname:
        doc_type = "Projection Tables"
    elif "mprfullreport" in fname or "mpr" in fname:
        doc_type = "Monetary Policy Report"
    elif "monetary" in fname:
        doc_type = "Monetary Document"
    elif "financial-stability-report" in fname or "financial" in fname:
        doc_type = "Financial Stability Report"
    elif "minutes" in fname or "fomcminutes" in fname:
        doc_type = "FOMC Minutes"
    else:
        doc_type = "FOMC Document"
    return f"{doc_type} - {date_str}"

def create_direct_link(file_name: str) -> str:
    try:
        base = "https://www.federalreserve.gov"
        name = file_name.split("/")[-1]
        mapping = [
            (r"beigebook", f"{base}/monetarypolicy/files/"),
            (r"fomc_longerungoals", f"{base}/monetarypolicy/files/"),
            (r"fomcprojtabl", f"{base}/monetarypolicy/files/"),
            (r"fomcpresconf", f"{base}/mediacenter/files/"),
            (r"presconf", f"{base}/mediacenter/files/"),
            (r"monetary", f"{base}/monetarypolicy/files/"),
            (r"financial-stability-report", f"{base}/publications/files/"),
            (r"mprfullreport", f"{base}/monetarypolicy/files/"),
            (r"fomcminutes", f"{base}/monetarypolicy/files/"),
        ]
        lower = name.lower()
        for pattern, prefix in mapping:
            if pattern in lower:
                return prefix + name
        return f"{base}/monetarypolicy/files/{name}"
    except Exception as e:
        logging.error(f"create_direct_link failed for {file_name}: {e}")
        return f"https://www.federalreserve.gov/monetarypolicy/files/{file_name.split('/')[-1]}"

# RETRIEVER CLASS
class CortexSearchRetriever:
    def __init__(self, snowpark_session: Session, limit: int = 12):
        self._session = snowpark_session
        self._limit = limit

    def retrieve(self, query: str) -> List[Dict[str, Any]]:
        safe_query = query.replace("'", "''")

        # FIX 4: Reduced over-fetch from limit*3 (36) to limit+6 (18)
        config = {
            "query": safe_query,
            "columns": ["CHUNK", "FILE_NAME"],
            "limit": self._limit + 6
        }
        config_json = json.dumps(config).replace("'", "''")

        sql = f"""
            SELECT PARSE_JSON(
                SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
                    'CORTEX_SEARCH_TUTORIAL_DB.PUBLIC.FOMC_SEARCH_SERVICE',
                    '{config_json}'
                )
            )['results'] AS results
        """

        try:
            df = self._session.sql(sql).collect()
            if not df:
                return []

            raw_results = json.loads(df[0]['RESULTS'])

            unique_docs = {}
            for r in raw_results:
                file_name = r.get('FILE_NAME', '')
                if file_name and file_name not in unique_docs:
                    unique_docs[file_name] = {
                        'chunk': r.get('CHUNK', ''),
                        'file_name': file_name
                    }

            docs = list(unique_docs.values())

            target_years = extract_target_years(query)
            if target_years:
                lower_year = min(target_years) - 1
                upper_year = max(target_years)
                docs = [d for d in docs if lower_year <= extract_file_year(d['file_name']) <= upper_year]

            docs.sort(key=lambda d: extract_file_year(d['file_name']), reverse=True)
            return docs[:self._limit]

        except Exception as e:
            logging.error(f"Retrieval error: {e}")
            return []

rag_retriever = CortexSearchRetriever(session)

# PROMPT GENERATION
# FIX 3: Pre-build static prompt header once at module load — never rebuilt per call
glossary = """
Glossary:
- Dot Plot: A chart showing each FOMC participant's forecast for the federal funds rate.
- Longer-run Inflation Expectations: Fed members' inflation expectations beyond the near-term future.
- Beige Book: A report summarizing economic conditions across Fed districts, published 8 times/year.
- Federal Funds Rate Target: The interest rate that the Fed targets for overnight lending between banks.
"""

_PROMPT_HEADER = f"""You are an expert economic analyst specializing in Federal Reserve communications.
{glossary}
Use ONLY the following excerpts from FOMC documents to answer the user's question. Do not invent facts. When relevant, cite the document type and year (e.g., "According to the January 2025 FOMC Minutes...").
If no direct context is available, provide a partial answer based on related information from other years or documents, clearly stating any assumptions (e.g., "Assuming trends from 2024 continue...").
If insufficient, respond: "Insufficient information in the provided documents. Please check the Federal Reserve website for more details."
"""

def build_system_prompt(query: str, contexts: List[dict], conversation_history: str = "") -> str:
    """
    Build system prompt with cached static header and dynamic context/history sections.
    """
    # Group contexts by year (limit to top 5)
    year_buckets = {}
    for c in contexts[:5]:
        year = extract_file_year(c["file_name"])
        year_buckets.setdefault(year, []).append(clean_chunk(c["chunk"]))

    grouped_texts = []
    for year in sorted(year_buckets.keys()):
        grouped_texts.append(f"Year {year} excerpts:\n{chr(10).join(year_buckets[year])}")

    context_text = "\n\n".join(grouped_texts)

    # FIX 3: Raised context limit from 1500 to 2500 chars for better grounding
    if len(context_text) > 2500:
        context_text = context_text[:2500]

    history_section = ""
    if conversation_history:
        history_section = f"\n\nRecent conversation:\n{conversation_history}\n"

    today = datetime.now().strftime("%B %d, %Y")

    prompt = f"""{_PROMPT_HEADER}
Today is {today}.

Context excerpts by year:
{context_text}
{history_section}
User Question: {query}
Answer:"""

    return prompt

# LLM COMPLETION
def retrieve_with_timeout(query: str, timeout: float = 25.0, retries: int = 1) -> List[dict]:
    """
    Wrap rag_retriever.retrieve with timeout, retry, and caching.
    """
    if not query:
        return []

    def normalize_query(q):
        return re.sub(r'[^\w\s]', '', q.lower()).strip()

    norm_query = normalize_query(query)

    if norm_query in st.session_state.rag_cache:
        return st.session_state.rag_cache[norm_query]

    def _call():
        return rag_retriever.retrieve(query)

    for attempt in range(retries + 1):
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(_call)
                results = future.result(timeout=timeout)
                cache_with_limit(st.session_state.rag_cache, norm_query, results)
                return results
        except concurrent.futures.TimeoutError:
            logging.warning(f"Retrieval timed out (attempt {attempt+1}/{retries+1})")
            time.sleep(1)
        except Exception as e:
            logging.error(f"Retrieval error (attempt {attempt+1}/{retries+1}): {e}")
            time.sleep(1)

    fallback = st.session_state.rag_cache.get(norm_query, [])
    if fallback:
        logging.warning("Using cached retrieval results as fallback.")
    return fallback

def cortex_complete_sql(session, model, prompt):
    """Call Cortex COMPLETE via SQL to avoid REST API 403 errors."""
    from snowflake.snowpark.functions import lit, call_function
    result = session.create_dataframe([('x',)], schema=['dummy']).select(
        call_function('SNOWFLAKE.CORTEX.COMPLETE', lit(model), lit(prompt)).alias('response')
    ).collect()
    return result[0]['RESPONSE']

def generate_response(query, contexts, conversation_history="", model="mistral-large2"):
    """
    FIX 1: Return the full response immediately — no fake word-by-word streaming.
    The previous version waited for the full LLM response then drizzled it out
    word-by-word, adding perceived latency with zero benefit.
    """
    prompt = build_system_prompt(query, contexts, conversation_history)
    try:
        return cortex_complete_sql(session, model, prompt)
    except Exception as e:
        logging.error(f"Cortex SQL error with {model}: {e}")
        try:
            prompt = build_system_prompt(query, contexts[:3], "")
            return cortex_complete_sql(session, "mixtral-8x7b", prompt)
        except Exception as e2:
            logging.error(f"Fallback completion failed: {e2}")
            return "I apologize, but I'm having trouble generating a response right now. Please try again."

def create_pdf(messages: List[dict]) -> BytesIO:
    """
    Creates a PDF document from the chat history.
    """
    buffer = BytesIO()

    now = datetime.now(ZoneInfo("America/New_York"))
    hour = now.strftime("%I").lstrip('0')
    am_pm = now.strftime("%p").lower()
    current_time = now.strftime(f"%B %d, %Y {hour}:%M {am_pm} EDT")

    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Chat History - {current_time}", styles["Title"]))
    story.append(Spacer(1, 12))

    for msg in messages:
        role = "User" if msg["role"] == "user" else "Assistant"
        content = html.escape(msg['content']).replace('\n', '<br/>')
        p_text = f"<b>{role}:</b><br/>{content}"
        story.append(Paragraph(p_text, styles["Normal"]))
        story.append(Spacer(1, 12))

        if msg["role"] == "assistant" and msg.get("contexts"):
            story.append(Paragraph("<b>Sources Used in Response:</b>", styles["Heading3"]))
            story.append(Spacer(1, 6))
            for c in msg["contexts"]:
                title = extract_clean_title(c['file_name'])
                link = create_direct_link(c['file_name'])
                source_text = f"• <a href='{link}' color='blue'>{title}</a>"
                story.append(Paragraph(source_text, styles["Normal"]))
                story.append(Spacer(1, 4))
            story.append(Spacer(1, 12))

    doc.build(story)
    buffer.seek(0)
    return buffer

def run_query(user_query: str):
    """
    Main query execution. FIX 1: renders response immediately without fake streaming loop.
    """
    start_time = time.time()
    conversation_history = get_recent_conversation_context(st.session_state.messages, max_pairs=2)

    # Retrieve context
    with st.spinner("Searching documents..."):
        contexts = retrieve_with_timeout(user_query, timeout=25.0, retries=1)
    retrieval_time = time.time() - start_time

    if not contexts:
        st.info("No relevant context found. Answering from general knowledge.")

    # Generate response — FIX 1: single call, render immediately
    with st.spinner("Generating response..."):
        response_text = generate_response(user_query, contexts, conversation_history)

    generation_time = time.time() - start_time - retrieval_time
    logging.info(f"Retrieval: {retrieval_time:.2f}s | Generation: {generation_time:.2f}s")

    # Render immediately — no token-by-token loop
    assistant_container = st.chat_message("assistant", avatar="🤖")
    assistant_container.markdown(response_text)

    top_contexts = contexts[:5] if contexts else []
    st.session_state.messages.append({
        "role": "assistant",
        "content": response_text,
        "contexts": top_contexts
    })

    # Limit message history
    if len(st.session_state.messages) > 10:
        st.session_state.messages = st.session_state.messages[-10:]

# INITIAL SETUP
st.set_page_config(
    page_title="Chat with the Federal Reserve",
    page_icon="💬",
    layout="centered"
)
st.markdown(
    """
    <div style='display: inline-flex; flex-direction: column; align-items: flex-end;'>
        <h2 style='margin: 0;'>🏦 Federal Reserve AI Research Assistant</h2>
        <div style='font-weight: bold; font-size: 18px;'>
            10,000 pages of Fed insights at your fingertips (2023 - 2026)
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True
)

# Initialize session state keys
if "messages" not in st.session_state:
    st.session_state.messages = []
if "rag_cache" not in st.session_state:
    st.session_state.rag_cache = LRUCache(maxsize=20)

# STREAMLIT UI LOGIC
# Display chat history
for msg in st.session_state.messages:
    if msg["role"] in ["user", "assistant"]:
        st.chat_message(msg["role"], avatar="👤" if msg["role"] == "user" else "🤖").markdown(msg["content"], unsafe_allow_html=False)

# After displaying all messages, show context and buttons for the LAST assistant message
if st.session_state.messages and st.session_state.messages[-1]["role"] == "assistant":
    top_contexts = st.session_state.messages[-1].get("contexts", [])
    with st.expander("📄 View References (top 5)", expanded=False):
        if not top_contexts:
            st.markdown("No relevant documents found. Check https://www.federalreserve.gov.")
        else:
            for c in top_contexts:
                title = extract_clean_title(c["file_name"])
                pdf_url = create_direct_link(c["file_name"])
                snippet = clean_chunk(c["chunk"])[:350] + ("..." if len(c["chunk"]) > 350 else "")
                st.markdown(f"**[{title}]({pdf_url})**")
                st.caption(snippet)
                st.divider()

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🧹 Clear Conversation"):
            st.session_state.messages.clear()
            st.session_state.rag_cache.clear()
            st.rerun()
    with col2:
        pdf_buffer = create_pdf(st.session_state.messages)
        st.download_button("📥 Download Chat History", pdf_buffer, "chat_history.pdf", "application/pdf")

# Chat input
user_input = st.chat_input("Ask the Fed about policy, inflation, outlooks, insights, or history...")
if user_input:
    st.chat_message("user", avatar="👤").write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input, "contexts": []})
    run_query(user_input)
    st.rerun()

# Example questions in sidebar
st.sidebar.markdown(
    """
    <h3 style='text-align: right;'>Example Questions</h3>
    """,
    unsafe_allow_html=True
)
example_questions = [
    "What will be the long-term impact of AI and automation on productivity, wage growth, and the overall demand for labor?",
    "What are greatest risks to financial stability over the next 12–18 months, and how are you monitoring them?",
    "Are businesses still struggling with costs?",
    "What's the median rate projection for next year?",
    "What's the Fed's plan going forward?",
    "To what extent do tariff policy and trade disruptions factor into your inflation outlook and decision-making?",
    "When and how fast should the Fed cut rates (if at all)?",
    "How exposed is the financial system to a shift in sentiment or asset revaluation?",
    "Are supply chain issues still showing up regionally?",
    "How did the FOMC view the economic outlook in mid-2023?",
    "What were the key points discussed in the FOMC meeting in January 2023?",
    "How did the FOMC assess the labor market in mid-2024?",
    "What was the fed funds rate target range effective September 19, 2024?",
]
for question in example_questions:
    if st.sidebar.button(question, key=f"example_{question[:50]}"):
        st.chat_message("user", avatar="👤").write(question)
        st.session_state.messages.append({"role": "user", "content": question, "contexts": []})
        run_query(question)
        st.rerun()
