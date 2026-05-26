"""
FastAPI Backend — QuantEdge Alpha Market Predictions
=====================================================
Serves prediction data endpoints and a streaming LLM agent for the AI Explorer.
Agent graph (messages → tool calls → responses) is wired for Domino GenAI tracing.
"""

import asyncio
import json
import os
import queue
import threading
from pathlib import Path
from typing import List, Optional

import anthropic
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Domino GenAI tracing — graceful fallback if SDK not present ───────────────
try:
    import mlflow
    from mlflow.entities import SpanType
    from domino.agents.tracing import add_tracing, init_tracing
    from domino.agents.logging import DominoRun

    init_tracing(autolog_frameworks=["anthropic"])
    TRACING_AVAILABLE = True
    print("✓ Domino GenAI tracing enabled")
except ImportError:
    TRACING_AVAILABLE = False
    print("⚠ Domino GenAI tracing not available — running in local mode")

    class _NoopSpan:
        def set_inputs(self, *a, **kw):  pass
        def set_outputs(self, *a, **kw): pass
        def __enter__(self):             return self
        def __exit__(self, *a):          pass

    class _NoopMlflow:
        @staticmethod
        def trace(span_type=None, name=None, **kw):
            return lambda f: f
        @staticmethod
        def start_span(name=None, span_type=None, **kw):
            return _NoopSpan()
        @staticmethod
        def get_current_active_span():
            return None

    mlflow    = _NoopMlflow()

    class SpanType:
        TOOL = "TOOL"

    def add_tracing(name=None, **kw):
        return lambda f: f

    class DominoRun:
        def __enter__(self): return self
        def __exit__(self, *a): pass


# ── Config ────────────────────────────────────────────────────────────────────
PREDICTIONS_DIR = Path(os.environ.get("PREDICTIONS_DIR",
                                      "/mnt/data/market-predictions-explorer"))
STATIC_DIR      = Path(__file__).parent / "static"

# ── Agent system prompt ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are QuantEdge Alpha, an AI investment analyst for a quantitative hedge fund.
You analyse outputs from our proprietary XGBoost market prediction model, trained on
5 years of daily technical indicator data across 15 equity symbols.

Capabilities you help with:
• Interpret today's trading signals and confidence scores
• Explain which technical factors (RSI, MACD, Bollinger Bands, momentum, volume) drive
  each prediction — in plain language a portfolio manager will appreciate
• Compare opportunities across sectors (Technology, Financials, Energy, Healthcare, ETF)
• Evaluate model performance through backtest metrics (Sharpe ratio, win rate, drawdown)
• Identify the highest-conviction opportunities from the current signal set

Communication style:
• Cite exact numbers — confidence %, RSI levels, Sharpe ratios, returns
• Explain technical indicators clearly; don't assume the reader is a quant
• Always distinguish model-generated signals from investment advice
• Be concise and actionable — one senior PM insight per response, not an essay

Always retrieve live data via your tools before answering; never fabricate numbers.\
"""

# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_current_signals",
        "description": (
            "Retrieve today's full signal table: ticker, sector, direction (BUY/SELL/HOLD), "
            "confidence score, key technical driver, last price, 1-day return, RSI-14, "
            "MACD histogram, Bollinger Band position, 20-day momentum, volume ratio, volatility."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_backtest_metrics",
        "description": (
            "Retrieve backtest performance over the ~1-year test period: Sharpe ratio, "
            "max drawdown, win rate, strategy total return, benchmark (SPY) return, "
            "model accuracy, AUC, and per-ticker accuracy breakdown."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_ticker_detail",
        "description": (
            "Get the full signal detail for a specific ticker — all feature values, "
            "predicted direction, confidence score, and key driver explanation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL, NVDA, JPM",
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_sector_summary",
        "description": (
            "Aggregate today's signals by sector. For each sector returns: "
            "number of BUY / SELL / HOLD signals and average confidence score."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_top_opportunities",
        "description": (
            "Return the N highest-confidence trading opportunities, "
            "optionally filtered to a specific direction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "How many opportunities to return (default 5)",
                },
                "direction": {
                    "type": "string",
                    "enum": ["BUY", "SELL", "HOLD", "ALL"],
                    "description": "Filter by signal direction (default ALL)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_feature_importance",
        "description": (
            "Return the model's global feature importance ranking — "
            "which technical indicators carry the most predictive power."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Data helpers ──────────────────────────────────────────────────────────────
def _load(filename: str) -> Optional[dict]:
    path = PREDICTIONS_DIR / filename
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# ── Tool implementations (each traced as a TOOL span) ─────────────────────────
@mlflow.trace(span_type=SpanType.TOOL, name="get_current_signals")
def tool_get_current_signals() -> dict:
    data = _load("signals.json")
    if data is None:
        return {"error": "Signals not yet generated — run the Domino Flow pipeline first."}
    return data


@mlflow.trace(span_type=SpanType.TOOL, name="get_backtest_metrics")
def tool_get_backtest_metrics() -> dict:
    data = _load("metrics.json")
    if data is None:
        return {"error": "Metrics not yet generated — run the Domino Flow pipeline first."}
    return data


@mlflow.trace(span_type=SpanType.TOOL, name="get_ticker_detail")
def tool_get_ticker_detail(ticker: str) -> dict:
    data = _load("signals.json")
    if data is None:
        return {"error": "Signals not yet generated."}
    matches = [s for s in data.get("signals", [])
               if s["ticker"].upper() == ticker.upper()]
    if not matches:
        return {"error": f"No signal found for '{ticker}'. "
                         f"Known tickers: AAPL MSFT NVDA GOOGL META AMZN JPM GS BAC XOM CVX JNJ UNH SPY QQQ"}
    return matches[0]


@mlflow.trace(span_type=SpanType.TOOL, name="get_sector_summary")
def tool_get_sector_summary() -> dict:
    data = _load("signals.json")
    if data is None:
        return {"error": "Signals not yet generated."}
    summary: dict = {}
    for s in data.get("signals", []):
        sec = s["sector"]
        if sec not in summary:
            summary[sec] = {"BUY": 0, "SELL": 0, "HOLD": 0, "_confs": []}
        summary[sec][s["direction"]] += 1
        summary[sec]["_confs"].append(s["confidence"])
    for sec in summary:
        confs = summary[sec].pop("_confs")
        summary[sec]["avg_confidence"] = round(
            sum(confs) / len(confs), 3) if confs else 0
    return summary


@mlflow.trace(span_type=SpanType.TOOL, name="get_top_opportunities")
def tool_get_top_opportunities(n: int = 5, direction: str = "ALL") -> list:
    data = _load("signals.json")
    if data is None:
        return [{"error": "Signals not yet generated."}]
    sigs = data.get("signals", [])
    if direction != "ALL":
        sigs = [s for s in sigs if s["direction"] == direction]
    return sorted(sigs, key=lambda s: s["confidence"], reverse=True)[:n]


@mlflow.trace(span_type=SpanType.TOOL, name="get_feature_importance")
def tool_get_feature_importance() -> dict:
    data = _load("feature_importance.json")
    if data is None:
        return {"error": "Feature importance not yet generated."}
    return data


def _dispatch(name: str, inputs: dict) -> str:
    dispatch_map = {
        "get_current_signals":  lambda: tool_get_current_signals(),
        "get_backtest_metrics": lambda: tool_get_backtest_metrics(),
        "get_ticker_detail":    lambda: tool_get_ticker_detail(inputs.get("ticker", "")),
        "get_sector_summary":   lambda: tool_get_sector_summary(),
        "get_top_opportunities":lambda: tool_get_top_opportunities(
                                    n=int(inputs.get("n", 5)),
                                    direction=str(inputs.get("direction", "ALL")),
                                ),
        "get_feature_importance": lambda: tool_get_feature_importance(),
    }
    fn = dispatch_map.get(name)
    result = fn() if fn else {"error": f"Unknown tool: {name}"}
    return json.dumps(result)


def _tool_status(name: str, inputs: dict) -> str:
    return {
        "get_current_signals":   "Fetching today's market signals…",
        "get_backtest_metrics":  "Loading backtest performance metrics…",
        "get_ticker_detail":     f"Analysing {inputs.get('ticker', '')} signal detail…",
        "get_sector_summary":    "Aggregating signals by sector…",
        "get_top_opportunities": "Finding highest-conviction opportunities…",
        "get_feature_importance":"Loading feature importance rankings…",
    }.get(name, f"Running {name}…")


# ── Agent ─────────────────────────────────────────────────────────────────────
@add_tracing(name="quant_edge_analyst")
def _run_agent(message: str, history: list, on_tool=None) -> dict:
    """Agentic LLM loop — fully traced by Domino GenAI tracing."""
    trace_id = None
    if TRACING_AVAILABLE:
        try:
            span = mlflow.get_current_active_span()
            if span:
                trace_id = span.request_id
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "reply": (
                "AI chat is not configured. "
                "Set the **ANTHROPIC_API_KEY** environment variable in your Domino workspace."
            ),
            "trace_id": None,
        }

    client = anthropic.Anthropic(api_key=api_key)

    messages = []
    for m in (history or [])[-8:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": message})

    for _ in range(6):   # max tool-call rounds
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return {"reply": block.text, "trace_id": trace_id}
            return {"reply": "", "trace_id": trace_id}

        if response.stop_reason == "tool_use":
            messages.append({
                "role":    "assistant",
                "content": [b.model_dump() for b in response.content],
            })
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if on_tool:
                        on_tool(block.name, block.input)
                    result = _dispatch(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

    return {"reply": "I couldn't complete the analysis in time. Please try again.",
            "trace_id": trace_id}


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="QuantEdge Alpha API")


class ChatMessage(BaseModel):
    role:    str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[ChatMessage]] = []


class FeedbackRequest(BaseModel):
    trace_id: str
    value:    int   # 1 = thumbs up, 0 = thumbs down


@app.get("/api/signals")
def get_signals():
    data = _load("signals.json")
    if data is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Pipeline not yet run. Trigger the Domino Flow first."},
        )
    return data


@app.get("/api/backtest")
def get_backtest():
    data = _load("backtest.json")
    if data is None:
        return JSONResponse(status_code=404, content={"error": "No backtest data yet."})
    return data


@app.get("/api/metrics")
def get_metrics():
    data = _load("metrics.json")
    if data is None:
        return JSONResponse(status_code=404, content={"error": "No metrics yet."})
    return data


@app.get("/api/feature_importance")
def get_feature_importance():
    data = _load("feature_importance.json")
    if data is None:
        return JSONResponse(status_code=404, content={"error": "No feature importance data yet."})
    return data


@app.post("/api/chat")
async def chat(request: ChatRequest):
    status_q: queue.Queue = queue.Queue()

    def on_tool(name: str, inputs: dict):
        status_q.put({"type": "status", "message": _tool_status(name, inputs)})

    def worker():
        try:
            with DominoRun():
                history = [{"role": m.role, "content": m.content}
                           for m in (request.history or [])]
                result  = _run_agent(request.message, history, on_tool=on_tool)
            status_q.put({
                "type":     "done",
                "reply":    result["reply"],
                "trace_id": result.get("trace_id"),
            })
        except Exception as exc:
            status_q.put({"type": "done", "reply": f"Error: {exc}", "trace_id": None})
        finally:
            status_q.put(None)   # sentinel

    threading.Thread(target=worker, daemon=True).start()

    async def generate():
        loop = asyncio.get_event_loop()
        while True:
            event = await loop.run_in_executor(None, status_q.get)
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/feedback")
def feedback(request: FeedbackRequest):
    if not TRACING_AVAILABLE:
        return {"ok": False, "reason": "Tracing not available in this environment"}
    try:
        from domino.agents.logging import log_evaluation
        log_evaluation(
            trace_id=request.trace_id,
            name="user_feedback",
            value=float(request.value),
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ── Static files and catch-all ────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{full_path:path}")
def catch_all(full_path: str):
    return FileResponse(STATIC_DIR / "index.html")