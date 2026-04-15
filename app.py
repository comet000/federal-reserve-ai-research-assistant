import streamlit as st
import re
import logging
import concurrent.futures
import time
import html
from typing import List
from datetime import datetime
from zoneinfo import ZoneInfo
from snowflake.snowpark import Session
from snowflake.core import Root
from snowflake.cortex import complete
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
        # Use flat secrets as provided in Streamlit Cloud
        connection_parameters = {
            "account": st.secrets["account"],
            "user": st.secrets["user"],
            "password": st.secrets["password"],
            "warehouse": st.secrets["warehouse"],
            "database": st.secrets["database"],
            "schema": st.secrets["schema"],
            "role": st.secrets["role"],
            "session_parameters": {
                "SECONDARY_ROLES": st.secrets.get("secondary_roles", "NONE")
            }
        }
    except (KeyError, TypeError) as e:
        logging.error(f"Failed to load secrets from Streamlit: {e}")
        st.error("Failed to load Snowflake credentials from secrets. Please check your Streamlit Cloud secrets configuration.")
        # Stop execution if secrets are not found
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
        self._root = Root(snowpark_session)
        self._service = search_service
        self.limit = limit
    def retrieve(self, query: str) -> List[dict]:
        try:
            raw_results = self._service.search(
                query=query,
                columns=["chunk", "file_name"],
                limit=150
            ).results
            unique_docs = {}
            for r in raw_results:
                if r["file_name"] not in unique_docs:
                    unique_docs[r["file_name"]] = r
            docs = list(unique_docs.values())
            target_years = extract_target_years(query)
            if target_years:
                lower_year = min(target_years) - 1
                upper_year = max(target_years)
                filtered_docs = [d for d in docs if lower_year <= extract_file_year(d["file_name"]) <= upper_year]
                if filtered_docs:
                    docs = filtered_docs
            docs = sorted(docs, key=lambda d: extract_file_year(d["file_name"]), reverse=True)
            docs = docs[:self.limit]
            return [{"chunk": d["chunk"], "file_name": d["file_name"]} for d in docs]
        except Exception as e:
            logging.error(f"Cortex Search retrieval error: {e}")
            st.error(f"❌ Cortex Search Error: {e}")
            return []
rag_retriever = CortexSearchRetriever(session)

# PROMPT GENERATION (OPTIMIZED)
glossary = """
Glossary:
- Dot Plot: A chart showing each FOMC participant's forecast for the federal funds rate.
- Longer-run Inflation Expectations: Fed members' inflation expectations beyond the near-term future.
- Beige Book: A report summarizing economic conditions across Fed districts, published 8 times/year.
- Federal Funds Rate Target: The interest rate that the Fed targets for overnight lending between banks.
"""
def build_system_prompt(query: str, contexts: List[dict], conversation_history: str = "") -> str:
    """
    Build an optimized system prompt with limited context size and inference encouragement.
    """
    # Group contexts by year (limit to top 5 to reduce size)
    year_buckets = {}
    for c in contexts[:5]:
        year = extract_file_year(c["file_name"])
        if year not in year_buckets:
            year_buckets[year] = []
        year_buckets[year].append(clean_chunk(c["chunk"]))
  
    grouped_texts = []
    for year in sorted(year_buckets.keys()):
        grouped_texts.append(f"Year {year} excerpts:\n{chr(10).join(year_buckets[year])}")
  
    context_text = "\n\n".join(grouped_texts)
  
    # Strict character limit on context
    if len(context_text) > 1500:
        context_text = context_text[:1500]
    # Add conversation history if available
    history_section = ""
    if conversation_history:
        history_section = f"\n\nRecent conversation:\n{conversation_history}\n"
    prompt = f"""You are an expert economic analyst specializing in Federal Reserve communications.
Today is {datetime.now():%B %d, %Y}.
{glossary}
Use ONLY the following excerpts from FOMC documents to answer the user's question. Do not invent facts. When relevant, cite the document type and year (e.g., "According to the January 2025 FOMC Minutes...").
If no direct context is available, provide a partial answer based on related information from other years or documents, clearly stating any assumptions (e.g., "Assuming trends from 2024 continue...").
If insufficient, respond: "Insufficient information in the provided documents. Please check the Federal Reserve website for more details."
Context excerpts by year:
{context_text}
{history_section}
User Question: {query}
Answer:"""
  
    return prompt

# LLM COMPLETION (OPTIMIZED)
def retrieve_with_timeout(query: str, timeout: float = 25.0, retries: int = 1) -> List[dict]:
    """
    Wrap rag_retriever.retrieve with timeout, retry, and caching.
    """
    if not query:
        return []
    # Normalize query for cache
    def normalize_query(q):
        return re.sub(r'[^\w\s]', '', q.lower()).strip()
  
    norm_query = normalize_query(query)
    # Check cache first
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
    # Fallback to cached result if available
    fallback = st.session_state.rag_cache.get(norm_query, [])
    if fallback:
        logging.warning("Using cached retrieval results as fallback.")
    return fallback

def generate_response_stream(query: str, contexts: List[dict], conversation_history: str = "", model="claude-3-5-sonnet-latest"):
    """
    Streaming call with retries and fallback to faster model.
    """
    prompt = build_system_prompt(query, contexts, conversation_history)
    def run_completion(model_to_use):
        return complete(model_to_use, prompt, stream=True, session=session)
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_completion, model)
                return future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            logging.warning(f"Cortex response timed out (attempt {attempt+1}/{max_retries+1})")
            st.warning("Response took too long. Trying faster model...")
            try:
                # Fallback to faster model with fewer contexts
                prompt = build_system_prompt(query, contexts[:3], "")
                return iter([complete("mixtral-8x7b", prompt, session=session)])
            except Exception as e:
                logging.error(f"Fallback completion failed: {e}")
                return iter(["Insufficient information in the provided documents. Please check https://www.federalreserve.gov."])
        except Exception as e:
            logging.error(f"Cortex streaming error (attempt {attempt+1}/{max_retries+1}): {e}")
            time.sleep(2)
    # Final fallback
    try:
        logging.warning("Falling back to non-streaming completion.")
        prompt = build_system_prompt(query, contexts[:3], "")
        backup = complete("mixtral-8x7b", prompt, session=session)
        return iter([backup])
    except Exception as e:
        logging.error(f"Backup completion failed: {e}")
        return iter(["I apologize, but I'm having trouble generating a response right now. Please try again."])

def create_pdf(messages: List[dict]) -> BytesIO:
    """
    Creates a PDF document from the chat history.
    - Fixes missing bullet point content by processing full messages.
    - Fixes time formatting to remove leading zeros from hours.
    - Includes sources for each assistant response.
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
        
        # Issue 1 Fix: Preserve all response content by replacing newlines with <br/> tags.
        # This prevents the PDF parser from splitting multi-line responses incorrectly.
        content = html.escape(msg['content']).replace('\n', '<br/>')
        
        p_text = f"<b>{role}:</b><br/>{content}"
        story.append(Paragraph(p_text, styles["Normal"]))
        story.append(Spacer(1, 12))

        # Issue 3 Fix: Add sources after each assistant response, not just the last one.
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
    Main query execution with logging for production monitoring.
    """
    start_time = time.time()
    conversation_history = get_recent_conversation_context(st.session_state.messages, max_pairs=2)
  
    # Retrieve context
    with st.spinner("Searching documents..."):
        contexts = retrieve_with_timeout(user_query, timeout=25.0, retries=1)
    retrieval_time = time.time() - start_time
    if not contexts:
        st.info("No relevant context found. Answering from general knowledge.")
    
    # Generate response
    with st.spinner("Generating response..."):
        stream = generate_response_stream(user_query, contexts, conversation_history)
  
    response_text = ""
    assistant_container = st.chat_message("assistant", avatar="🤖")
    placeholder = assistant_container.empty()
  
    for token in stream:
        try:
            response_text += token
            placeholder.markdown(response_text, unsafe_allow_html=False)
        except Exception:
            logging.exception("Error while streaming chunk")
    generation_time = time.time() - start_time - retrieval_time
    
    # MODIFICATION: Attach the contexts used for this specific response to the message object.
    top_contexts = contexts[:5] if contexts else []
    st.session_state.messages.append({"role": "assistant", "content": response_text, "contexts": top_contexts})

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
        <h1 style='margin: 0;'>🏦 Chat with the Federal Reserve</h1>
        <div style='font-weight: bold; font-size: 18px;'>
            5,000 pages of Fed insights at your fingertips (2023 - 2025)
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# Hide Streamlit default menu and footer for cleaner UI
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

# After displaying all messages, show the context and buttons for the LAST assistant message.
if st.session_state.messages and st.session_state.messages[-1]["role"] == "assistant":
    top_contexts = st.session_state.messages[-1].get("contexts", [])
    with st.expander("📄 View Context (top 5)", expanded=False):
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
    # MODIFICATION: Add an empty 'contexts' list to user messages for consistent data structure.
    st.session_state.messages.append({"role": "user", "content": user_input, "contexts": []})
    run_query(user_input)
    st.rerun() # Rerun to display the context and buttons immediately.


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
        # MODIFICATION: Add an empty 'contexts' list to user messages for consistent data structure.
        st.session_state.messages.append({"role": "user", "content": question, "contexts": []})
        run_query(question)
        st.rerun() # Rerun to display the context and buttons immediately.

