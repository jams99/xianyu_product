#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "xianyu_agent.db"
HOST = "127.0.0.1"
PORT = 8765


RISK_KEYWORDS = {
    "电影票": "票务/卡券类商品可能存在平台类目和资质要求，发布前请确认规则与合法来源。",
    "演唱会": "演出票务风险较高，建议不要发布来源不确定或平台限制的商品。",
    "充值卡": "电子卡券类商品可能需要走指定频道或受类目限制，发布前请确认规则。",
    "卡密": "卡密交付要保留来源、有效期和售后说明，避免承诺无法兑现。",
    "优惠券": "优惠券需确认可转让、可使用、未违反发行方规则。",
}


AUTO_PRODUCT_CATALOG = [
    {
        "name": "视频会员月卡兑换权益",
        "category": "无物流数字权益",
        "keywords": "视频会员 月卡 兑换 权益",
        "cost": 11,
        "sample_prices": [13, 15, 16, 18, 20, 22],
        "notes": "接单后只采购可转让、可验证的兑换权益；确认有效期和适用平台后再交付。",
    },
    {
        "name": "音乐会员月卡兑换权益",
        "category": "无物流数字权益",
        "keywords": "音乐会员 月卡 兑换 权益",
        "cost": 8,
        "sample_prices": [10, 12, 13, 15, 18, 20],
        "notes": "接单后确认平台、有效期和是否可转让，采购低价兑换权益后交付。",
    },
    {
        "name": "网盘会员月卡兑换权益",
        "category": "无物流数字权益",
        "keywords": "网盘会员 月卡 兑换 权益",
        "cost": 9,
        "sample_prices": [11, 13, 15, 16, 19, 22],
        "notes": "接单后确认账号类型、有效期和使用限制，只交付可验证的兑换权益。",
    },
    {
        "name": "咖啡代金券兑换权益",
        "category": "无物流数字权益",
        "keywords": "咖啡 代金券 兑换券 权益",
        "cost": 7,
        "sample_prices": [9, 10, 12, 13, 15, 18],
        "notes": "接单后确认门店、有效期和可转让性，只采购可正常核销的兑换权益。",
    },
    {
        "name": "外卖代金券兑换权益",
        "category": "无物流数字权益",
        "keywords": "外卖 代金券 红包 兑换 权益",
        "cost": 5,
        "sample_prices": [7, 8, 9, 11, 13, 15],
        "notes": "接单后确认平台、城市、有效期和使用门槛，只采购可转让可核销权益。",
    },
    {
        "name": "游戏点卡小额兑换权益",
        "category": "无物流数字权益",
        "keywords": "游戏点卡 小额 兑换 权益",
        "cost": 8,
        "sample_prices": [10, 12, 13, 15, 17, 20],
        "notes": "接单后确认游戏区服、面额和兑换限制，只采购来源清晰的可转让权益。",
    },
]

AUTO_BLOCKED_KEYWORDS = {"电影票", "演唱会", "票务"}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists products (
                id integer primary key autoincrement,
                name text not null,
                category text not null default '',
                keywords text not null default '',
                cost real not null default 0,
                min_profit real not null default 3,
                risk_buffer real not null default 1,
                stock_mode text not null default 'after_order',
                status text not null default 'active',
                notes text not null default '',
                created_at integer not null
            );

            create table if not exists market_samples (
                id integer primary key autoincrement,
                product_id integer not null references products(id) on delete cascade,
                title text not null,
                price real not null,
                source text not null default '闲鱼',
                seller text not null default '',
                note text not null default '',
                created_at integer not null
            );

            create table if not exists drafts (
                id integer primary key autoincrement,
                product_id integer not null references products(id) on delete cascade,
                title text not null,
                price real not null,
                body text not null,
                warnings text not null default '[]',
                decision text not null default '',
                created_at integer not null
            );

            create table if not exists orders (
                id integer primary key autoincrement,
                product_id integer not null references products(id),
                buyer text not null default '',
                sale_price real not null,
                max_purchase_price real not null,
                status text not null default 'new',
                reply text not null default '',
                source_sample_id integer,
                created_at integer not null
            );

            create table if not exists opportunities (
                id integer primary key autoincrement,
                keyword text not null,
                category text not null default '',
                cost real not null default 0,
                min_profit real not null default 3,
                risk_buffer real not null default 1,
                sample_prices text not null default '[]',
                sample_count integer not null default 0,
                min_price real not null default 0,
                median_price real not null default 0,
                suggested_price real not null default 0,
                max_purchase_price real not null default 0,
                expected_profit real not null default 0,
                viable integer not null default 0,
                decision text not null default '',
                created_at integer not null
            );

            create table if not exists publish_queue (
                id integer primary key autoincrement,
                product_id integer not null references products(id) on delete cascade,
                title text not null,
                price real not null,
                body text not null,
                status text not null default 'ready',
                source text not null default 'autopilot',
                warnings text not null default '[]',
                created_at integer not null,
                updated_at integer not null
            );
            """
        )


def now() -> int:
    return int(time.time())


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def money(value: float) -> str:
    rounded = round(value + 1e-9, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def clean_float(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - pos) + ordered[upper] * (pos - lower)


def trimmed_mean(values: list[float]) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) >= 5:
        cut = max(1, int(len(ordered) * 0.1))
        ordered = ordered[cut:-cut] or ordered
    return statistics.fmean(ordered)


def risk_warnings(product: dict[str, Any]) -> list[str]:
    text = f"{product.get('name', '')} {product.get('category', '')} {product.get('keywords', '')} {product.get('notes', '')}"
    warnings: list[str] = []
    for keyword, warning in RISK_KEYWORDS.items():
        if keyword in text:
            warnings.append(warning)
    if product.get("stock_mode") == "after_order":
        warnings.append("当前是接单后采购模式，发布文案不要承诺现货秒发；建议写清楚确认后安排。")
    return warnings


@dataclass
class MarketAnalysis:
    sample_count: int
    min_price: float
    q1_price: float
    median_price: float
    trimmed_avg: float
    high_price: float
    recommended_markup: float
    suggested_price: float
    estimated_purchase_price: float
    max_purchase_price: float
    expected_profit: float
    viable: bool
    decision: str


def analyze_market(product: dict[str, Any], samples: list[dict[str, Any]]) -> MarketAnalysis:
    prices = [float(sample["price"]) for sample in samples if float(sample["price"]) > 0]
    min_profit = float(product["min_profit"])
    risk_buffer = float(product["risk_buffer"])
    cost = float(product["cost"])

    if not prices:
        floor = cost + min_profit + risk_buffer
        return MarketAnalysis(
            sample_count=0,
            min_price=0,
            q1_price=0,
            median_price=0,
            trimmed_avg=0,
            high_price=0,
            recommended_markup=2,
            suggested_price=math.ceil(floor),
            estimated_purchase_price=cost,
            max_purchase_price=0,
            expected_profit=0,
            viable=False,
            decision="先补充至少 5 条闲鱼行情样本，再发布测试。",
        )

    min_price = min(prices)
    q1_price = percentile(prices, 0.25)
    median_price = statistics.median(prices)
    avg = trimmed_mean(prices)
    high_price = percentile(prices, 0.75)
    spread = max(high_price - min_price, 0)
    competition = len(prices)

    if competition < 5:
        markup = 1
    elif spread >= 8:
        markup = 2
    else:
        markup = 1.5

    estimated_purchase = max(cost, min_price)
    floor_price = estimated_purchase + min_profit + risk_buffer
    market_anchor = max(median_price, avg) + markup
    suggested = math.ceil(max(floor_price, market_anchor))
    max_purchase = suggested - min_profit - risk_buffer
    expected_profit = suggested - estimated_purchase - risk_buffer
    viable = max_purchase >= estimated_purchase and expected_profit >= min_profit

    if len(prices) < 5:
        decision = "样本偏少，可以小量测试；建议先观察 5 条以上同类商品。"
    elif not viable:
        decision = "暂不建议发布：低价货源和目标利润之间空间不足。"
    elif suggested > median_price + 5 and spread < 5:
        decision = "可测试但要谨慎：建议价明显高于主流价，优先优化文案和信任感。"
    else:
        decision = "可以发布测试：价差空间满足最低利润和风险缓冲。"

    return MarketAnalysis(
        sample_count=len(prices),
        min_price=min_price,
        q1_price=q1_price,
        median_price=median_price,
        trimmed_avg=avg,
        high_price=high_price,
        recommended_markup=markup,
        suggested_price=suggested,
        estimated_purchase_price=estimated_purchase,
        max_purchase_price=max_purchase,
        expected_profit=expected_profit,
        viable=viable,
        decision=decision,
    )


def make_publish_draft(product: dict[str, Any], analysis: MarketAnalysis) -> dict[str, Any]:
    name = product["name"].strip()
    keywords = [part.strip() for part in re.split(r"[,，\s]+", product["keywords"]) if part.strip()]
    suffix = " ".join(keywords[:3])
    title = f"{name} {suffix} 可确认后安排".strip()
    if len(title) > 30:
        title = title[:30]

    body_lines = [
        f"商品：{name}",
        "适合不需要快递的虚拟权益/数字兑换类需求。",
        "下单前请先咨询，确认可用范围、有效期和当前可安排情况。",
        "确认后再安排低价货源采购与交付；如暂时没有合适货源，会及时说明缺货并不强行成交。",
        "不支持来源不明、违规用途或超出规则范围的使用方式。",
    ]
    if product["notes"].strip():
        body_lines.append(f"补充说明：{product['notes'].strip()}")
    body_lines.append(f"参考售价：{money(analysis.suggested_price)} 元")

    warnings = risk_warnings(product)
    return {
        "title": title,
        "price": analysis.suggested_price,
        "body": "\n".join(body_lines),
        "warnings": warnings,
        "decision": analysis.decision,
    }


def generate_reply(product: dict[str, Any], question: str, analysis: MarketAnalysis | None = None) -> str:
    q = question.strip()
    name = product["name"]
    lower = q.lower()

    if any(word in q for word in ["便宜", "优惠", "少点", "刀", "最低"]):
        price = money(analysis.suggested_price) if analysis else "当前标价"
        return f"可以先按 {price} 元看，确认可用范围后我再帮你安排。这个价格已经预留了采购和售后风险，不太适合大幅议价。"
    if any(word in q for word in ["有货", "现在", "多久", "发货", "什么时候"]):
        return f"{name} 需要先确认当前低价货源和可用范围。你把使用场景/地区/面额发我，我确认能安排再让你下单；如果没有合适货源会直接说缺货。"
    if any(word in q for word in ["怎么用", "使用", "有效期", "范围"]):
        return f"{name} 的使用范围、有效期和限制需要按具体货源确认。你先说下要用在哪里，我确认后给你完整说明，避免买错。"
    if any(word in lower for word in ["hi", "hello"]) or "你好" in q:
        return f"你好，{name} 下单前需要先确认可用范围和当前货源。你可以把需求发我，我确认能安排再继续。"
    return f"收到，我先按 {name} 帮你确认。为了避免无效交付，请补充一下使用范围、期望价格和是否急用；如果没有合适低价货源，我会直接回复缺货。"


def catalog_item_analysis(item: dict[str, Any], min_profit: float, risk_buffer: float) -> tuple[MarketAnalysis, dict[str, Any], list[dict[str, Any]]]:
    product = {
        "name": item["name"],
        "category": item["category"],
        "keywords": item["keywords"],
        "cost": item["cost"],
        "min_profit": min_profit,
        "risk_buffer": risk_buffer,
        "stock_mode": "after_order",
        "notes": item["notes"],
    }
    samples = [
        {
            "title": item["name"],
            "price": float(price),
            "note": f"自动候选参考价：{item['name']} {money(float(price))}元",
        }
        for price in item["sample_prices"]
    ]
    return analyze_market(product, samples), product, samples


def parse_price_lines(text: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.search(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?", line)
        if not match:
            continue
        price = float(match.group(1))
        title = (line[: match.start()] + line[match.end() :]).strip(" -|，,")
        if not title:
            title = line
        samples.append({"title": title[:120], "price": price, "note": line[:240]})
    return samples


def parse_opportunity_lines(text: str) -> list[dict[str, Any]]:
    opportunities: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            keyword, price_text = [part.strip() for part in line.split("|", 1)]
        elif "：" in line:
            keyword, price_text = [part.strip() for part in line.split("：", 1)]
        elif ":" in line:
            keyword, price_text = [part.strip() for part in line.split(":", 1)]
        else:
            match = re.search(r"(?:¥|￥)?\s*\d+(?:\.\d{1,2})?\s*(?:元)?", line)
            if not match:
                continue
            keyword = line[: match.start()].strip(" -|，,")
            price_text = line[match.start() :]
        prices = [
            float(match.group(1))
            for match in re.finditer(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?", price_text)
        ]
        if keyword and prices:
            opportunities.append({"keyword": keyword[:80], "prices": prices})
    return opportunities


def get_product(conn: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    row = conn.execute("select * from products where id = ?", (product_id,)).fetchone()
    return dict(row) if row else None


def get_samples(conn: sqlite3.Connection, product_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from market_samples where product_id = ? order by price asc, created_at desc",
        (product_id,),
    ).fetchall()
    return rows_to_dicts(rows)


def get_opportunities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from opportunities order by viable desc, expected_profit desc, created_at desc limit 30"
    ).fetchall()
    opportunities = rows_to_dicts(rows)
    for opportunity in opportunities:
        opportunity["viable"] = bool(opportunity["viable"])
        opportunity["sample_prices"] = json.loads(opportunity["sample_prices"] or "[]")
    return opportunities


def get_publish_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select pq.*, p.name as product_name, p.keywords
        from publish_queue pq
        join products p on p.id = pq.product_id
        order by case pq.status when 'ready' then 0 when 'filled' then 1 else 2 end, pq.created_at desc
        limit 30
        """
    ).fetchall()
    queue = rows_to_dicts(rows)
    for item in queue:
        item["warnings"] = json.loads(item["warnings"] or "[]")
    return queue


def app_state() -> dict[str, Any]:
    with connect() as conn:
        products = rows_to_dicts(conn.execute("select * from products order by created_at desc").fetchall())
        enriched: list[dict[str, Any]] = []
        for product in products:
            samples = get_samples(conn, product["id"])
            analysis = analyze_market(product, samples)
            draft = make_publish_draft(product, analysis)
            latest_draft = conn.execute(
                "select * from drafts where product_id = ? order by created_at desc limit 1",
                (product["id"],),
            ).fetchone()
            orders = rows_to_dicts(
                conn.execute(
                    "select * from orders where product_id = ? order by created_at desc",
                    (product["id"],),
                ).fetchall()
            )
            enriched.append(
                {
                    **product,
                    "samples": samples,
                    "analysis": analysis.__dict__,
                    "draft_preview": draft,
                    "latest_draft": dict(latest_draft) if latest_draft else None,
                    "orders": orders,
                }
            )
        return {
            "products": enriched,
            "opportunities": get_opportunities(conn),
            "publish_queue": get_publish_queue(conn),
        }


class AppHandler(BaseHTTPRequestHandler):
    server_version = "XianyuAgent/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
        elif parsed.path == "/api/state":
            self.send_json(app_state())
        elif parsed.path == "/collector.js":
            self.send_js(self.collector_script(parsed))
        elif parsed.path == "/publisher.js":
            self.send_js(self.publisher_script(parsed))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/market-samples":
                self.create_market_samples(payload)
            elif parsed.path == "/api/drafts":
                self.create_draft(payload)
            elif parsed.path == "/api/orders":
                self.create_order(payload)
            elif parsed.path == "/api/replies":
                self.create_reply(payload)
            elif parsed.path == "/api/opportunities":
                self.create_opportunities(payload)
            elif parsed.path == "/api/products/from-opportunity":
                self.create_product_from_opportunity(payload)
            elif parsed.path == "/api/autopilot/run":
                self.run_autopilot(payload)
            elif parsed.path == "/api/autopilot/cleanup-labor":
                self.cleanup_labor_products()
            elif parsed.path == "/api/publish-queue/status":
                self.update_publish_queue_status(payload)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_cors_headers()
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_js(self, script: str) -> None:
        encoded = script.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_cors_headers()
        self.send_header("content-type", "application/javascript; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_cors_headers(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")

    def collector_script(self, parsed: urllib.parse.ParseResult) -> str:
        query = urllib.parse.parse_qs(parsed.query)
        product_id = int((query.get("product_id") or ["0"])[0])
        return f"""
(async () => {{
  const productId = {product_id};
  if (!productId) {{
    alert("缺少 product_id，无法导入行情。");
    return;
  }}
  const lines = document.body.innerText
    .split(/\\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const priceLines = [];
  for (let index = 0; index < lines.length; index += 1) {{
    const line = lines[index];
    if (/(?:¥|￥)?\\s*\\d+(?:\\.\\d{{1,2}})?\\s*(?:元)?/.test(line)) {{
      const previous = lines[index - 1] || "";
      const next = lines[index + 1] || "";
      priceLines.push([previous, line, next].filter(Boolean).join(" "));
    }}
  }}
  if (!priceLines.length) {{
    alert("没有识别到价格。请先确认搜索结果已加载，或手动复制页面文本到后台。");
    return;
  }}
  const response = await fetch("http://127.0.0.1:{PORT}/api/market-samples", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      product_id: productId,
      bulk_text: priceLines.slice(0, 80).join("\\n"),
      source: location.href
    }})
  }});
  if (!response.ok) {{
    const data = await response.json().catch(() => ({{}}));
    alert(data.error || "导入失败，请回到本地后台查看。");
    return;
  }}
  alert(`已导入 ${{priceLines.length}} 条候选价格，请回到本地后台查看。`);
}})();
"""

    def publisher_script(self, parsed: urllib.parse.ParseResult) -> str:
        query = urllib.parse.parse_qs(parsed.query)
        product_id = int((query.get("product_id") or ["0"])[0])
        with connect() as conn:
            product = get_product(conn, product_id)
            if not product:
                draft = {"title": "", "price": 0, "body": "商品不存在", "warnings": [], "decision": ""}
            else:
                draft = make_publish_draft(product, analyze_market(product, get_samples(conn, product_id)))
        payload = json.dumps(draft, ensure_ascii=False)
        return f"""
(() => {{
  const draft = {payload};
  const isVisible = (el) => {{
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
  }};
  const labelText = (el) => [
    el.getAttribute("aria-label"),
    el.getAttribute("placeholder"),
    el.getAttribute("name"),
    el.getAttribute("id"),
    el.closest("label")?.innerText,
    el.parentElement?.innerText?.slice(0, 120)
  ].filter(Boolean).join(" ");
  const setValue = (el, value) => {{
    el.focus();
    if (el.isContentEditable) {{
      el.innerText = value;
    }} else {{
      el.value = value;
    }}
    el.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: String(value).slice(0, 20) }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    el.blur();
  }};
  const fields = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true'], [contenteditable='plaintext-only']"))
    .filter((el) => !el.disabled && !el.readOnly && isVisible(el));
  const pick = (patterns, fallback) => {{
    const scored = fields
      .map((el) => {{
        const text = labelText(el);
        const score = patterns.reduce((sum, pattern) => sum + (pattern.test(text) ? 1 : 0), 0);
        return {{ el, score }};
      }})
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);
    return scored[0]?.el || fallback();
  }};
  const textInputs = fields.filter((el) => el.tagName === "INPUT" && !["hidden", "file", "checkbox", "radio"].includes((el.type || "").toLowerCase()));
  const titleField = pick([/标题|名称|宝贝|商品|闲置/], () => textInputs[0]);
  const priceField = pick([/价格|售价|金额|价钱/], () => textInputs.find((el) => ["number", "tel", "text"].includes((el.type || "text").toLowerCase())));
  const bodyField = pick([/描述|详情|介绍|说明|内容/], () => fields.find((el) => el.tagName === "TEXTAREA" || el.isContentEditable));
  const filled = [];
  if (titleField) {{
    setValue(titleField, draft.title);
    filled.push("标题");
  }}
  if (priceField) {{
    setValue(priceField, String(draft.price));
    filled.push("价格");
  }}
  if (bodyField) {{
    setValue(bodyField, draft.body);
    filled.push("描述");
  }}
  alert(
    filled.length
      ? `已尝试填入：${{filled.join("、")}}。请人工检查类目、图片、规则提示和最终发布按钮。`
      : "没有找到可填写的标题/价格/描述输入框，请确认已进入发布编辑页。"
  );
}})();
"""

    def create_market_samples(self, payload: dict[str, Any]) -> None:
        product_id = int(payload.get("product_id", 0))
        text = payload.get("bulk_text", "")
        manual_title = payload.get("title", "").strip()
        manual_price = clean_float(payload.get("price"), 0)
        source = payload.get("source", "闲鱼").strip() or "闲鱼"
        with connect() as conn:
            product = get_product(conn, product_id)
            if not product:
                raise ValueError("商品不存在")
            samples = parse_price_lines(text)
            if manual_title and manual_price > 0:
                samples.append({"title": manual_title, "price": manual_price, "note": payload.get("note", "").strip()})
            if not samples:
                raise ValueError("没有识别到有效价格")
            conn.executemany(
                """
                insert into market_samples (product_id, title, price, source, seller, note, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        product_id,
                        sample["title"],
                        sample["price"],
                        source,
                        payload.get("seller", "").strip(),
                        sample.get("note", ""),
                        now(),
                    )
                    for sample in samples
                ],
            )
        self.send_json(app_state(), HTTPStatus.CREATED)

    def create_draft(self, payload: dict[str, Any]) -> None:
        product_id = int(payload.get("product_id", 0))
        with connect() as conn:
            product = get_product(conn, product_id)
            if not product:
                raise ValueError("商品不存在")
            samples = get_samples(conn, product_id)
            analysis = analyze_market(product, samples)
            draft = make_publish_draft(product, analysis)
            conn.execute(
                """
                insert into drafts (product_id, title, price, body, warnings, decision, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    draft["title"],
                    draft["price"],
                    draft["body"],
                    json.dumps(draft["warnings"], ensure_ascii=False),
                    draft["decision"],
                    now(),
                ),
            )
        self.send_json(app_state(), HTTPStatus.CREATED)

    def create_order(self, payload: dict[str, Any]) -> None:
        product_id = int(payload.get("product_id", 0))
        sale_price = clean_float(payload.get("sale_price"), 0)
        buyer = payload.get("buyer", "").strip()
        fresh_sources = parse_price_lines(payload.get("fresh_sources", ""))
        with connect() as conn:
            product = get_product(conn, product_id)
            if not product:
                raise ValueError("商品不存在")
            if fresh_sources:
                conn.executemany(
                    """
                    insert into market_samples (product_id, title, price, source, seller, note, created_at)
                    values (?, ?, ?, '履约搜索', '', ?, ?)
                    """,
                    [
                        (
                            product_id,
                            sample["title"],
                            sample["price"],
                            sample.get("note", ""),
                            now(),
                        )
                        for sample in fresh_sources
                    ],
                )
            samples = get_samples(conn, product_id)
            analysis = analyze_market(product, samples)
            if sale_price <= 0:
                sale_price = analysis.suggested_price
            max_purchase = sale_price - float(product["min_profit"]) - float(product["risk_buffer"])
            candidates = [sample for sample in samples if float(sample["price"]) <= max_purchase]
            candidates.sort(key=lambda item: float(item["price"]))
            if candidates:
                chosen = candidates[0]
                ranked = "；".join(
                    f"{index + 1}. {item['title']} {money(float(item['price']))}元"
                    for index, item in enumerate(candidates[:5])
                )
                reply = (
                    f"这单可以处理。最高采购价 {money(max_purchase)} 元，"
                    f"优先找：{chosen['title']}（{money(float(chosen['price']))} 元）。"
                    f"候选排序：{ranked}。人工付款确认后再交付。"
                )
                status = "source_found"
                source_id = chosen["id"]
            else:
                reply = (
                    f"抱歉，当前没有找到低于 {money(max_purchase)} 元采购上限的合适货源，暂时缺货。"
                    "建议不要让买家付款或及时协商取消。"
                )
                status = "out_of_stock"
                source_id = None
            conn.execute(
                """
                insert into orders (product_id, buyer, sale_price, max_purchase_price, status, reply, source_sample_id, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, buyer, sale_price, max_purchase, status, reply, source_id, now()),
            )
        self.send_json(app_state(), HTTPStatus.CREATED)

    def create_reply(self, payload: dict[str, Any]) -> None:
        product_id = int(payload.get("product_id", 0))
        question = payload.get("question", "").strip()
        with connect() as conn:
            product = get_product(conn, product_id)
            if not product:
                raise ValueError("商品不存在")
            analysis = analyze_market(product, get_samples(conn, product_id))
        self.send_json({"reply": generate_reply(product, question, analysis)})

    def create_opportunities(self, payload: dict[str, Any]) -> None:
        text = payload.get("bulk_text", "")
        category = payload.get("category", "").strip()
        cost = clean_float(payload.get("cost"), 0)
        min_profit = clean_float(payload.get("min_profit"), 3)
        risk_buffer = clean_float(payload.get("risk_buffer"), 1)
        parsed = parse_opportunity_lines(text)
        if not parsed:
            raise ValueError("没有识别到机会数据，请使用“关键词 | 15元 16元 18元”的格式")
        with connect() as conn:
            for item in parsed:
                product = {
                    "name": item["keyword"],
                    "category": category,
                    "keywords": item["keyword"],
                    "cost": cost,
                    "min_profit": min_profit,
                    "risk_buffer": risk_buffer,
                    "stock_mode": "after_order",
                    "notes": "",
                }
                samples = [
                    {"title": item["keyword"], "price": price, "note": f"{item['keyword']} {money(price)}元"}
                    for price in item["prices"]
                ]
                analysis = analyze_market(product, samples)
                conn.execute(
                    """
                    insert into opportunities (
                        keyword, category, cost, min_profit, risk_buffer, sample_prices, sample_count,
                        min_price, median_price, suggested_price, max_purchase_price,
                        expected_profit, viable, decision, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["keyword"],
                        category,
                        cost,
                        min_profit,
                        risk_buffer,
                        json.dumps(item["prices"], ensure_ascii=False),
                        len(item["prices"]),
                        analysis.min_price,
                        analysis.median_price,
                        analysis.suggested_price,
                        analysis.max_purchase_price,
                        analysis.expected_profit,
                        1 if analysis.viable else 0,
                        analysis.decision,
                        now(),
                    ),
                )
        self.send_json(app_state(), HTTPStatus.CREATED)

    def create_product_from_opportunity(self, payload: dict[str, Any]) -> None:
        opportunity_id = int(payload.get("opportunity_id", 0))
        with connect() as conn:
            row = conn.execute("select * from opportunities where id = ?", (opportunity_id,)).fetchone()
            if not row:
                raise ValueError("机会不存在")
            opportunity = dict(row)
            cursor = conn.execute(
                """
                insert into products (name, category, keywords, cost, min_profit, risk_buffer, stock_mode, notes, created_at)
                values (?, ?, ?, ?, ?, ?, 'after_order', ?, ?)
                """,
                (
                    opportunity["keyword"],
                    opportunity["category"],
                    opportunity["keyword"],
                    opportunity["cost"],
                    opportunity["min_profit"],
                    opportunity["risk_buffer"],
                    "由机会扫描转入，请发布前确认平台规则和合法来源。",
                    now(),
                ),
            )
            product_id = cursor.lastrowid
            prices = json.loads(opportunity["sample_prices"] or "[]")
            conn.executemany(
                """
                insert into market_samples (product_id, title, price, source, seller, note, created_at)
                values (?, ?, ?, '机会扫描', '', ?, ?)
                """,
                [
                    (
                        product_id,
                        opportunity["keyword"],
                        float(price),
                        f"机会扫描导入：{opportunity['keyword']} {money(float(price))}元",
                        now(),
                    )
                    for price in prices
                ],
            )
        self.send_json(app_state(), HTTPStatus.CREATED)

    def run_autopilot(self, payload: dict[str, Any]) -> None:
        max_items = max(1, min(6, int(clean_float(payload.get("max_items"), 3))))
        min_profit = clean_float(payload.get("min_profit"), 3)
        risk_buffer = clean_float(payload.get("risk_buffer"), 1)
        candidates: list[tuple[float, dict[str, Any], dict[str, Any], list[dict[str, Any]], MarketAnalysis]] = []
        for item in AUTO_PRODUCT_CATALOG:
            analysis, product, samples = catalog_item_analysis(item, min_profit, risk_buffer)
            catalog_text = f"{item['name']} {item['category']} {item['keywords']} {item['notes']}"
            is_blocked = any(keyword in catalog_text for keyword in AUTO_BLOCKED_KEYWORDS)
            if analysis.viable and not is_blocked:
                score = analysis.expected_profit + (analysis.sample_count * 0.2) - (analysis.suggested_price * 0.03)
                candidates.append((score, item, product, samples, analysis))
        candidates.sort(key=lambda entry: entry[0], reverse=True)

        created = 0
        with connect() as conn:
            existing_names = {
                row["name"]
                for row in conn.execute("select name from products").fetchall()
            }
            for _score, item, product, samples, analysis in candidates:
                if created >= max_items:
                    break
                if product["name"] in existing_names:
                    continue
                cursor = conn.execute(
                    """
                    insert into products (name, category, keywords, cost, min_profit, risk_buffer, stock_mode, notes, created_at)
                    values (?, ?, ?, ?, ?, ?, 'after_order', ?, ?)
                    """,
                    (
                        product["name"],
                        product["category"],
                        product["keywords"],
                        product["cost"],
                        product["min_profit"],
                        product["risk_buffer"],
                        product["notes"],
                        now(),
                    ),
                )
                product_id = cursor.lastrowid
                conn.executemany(
                    """
                    insert into market_samples (product_id, title, price, source, seller, note, created_at)
                    values (?, ?, ?, '后台自动选品', '', ?, ?)
                    """,
                    [
                        (product_id, sample["title"], sample["price"], sample["note"], now())
                        for sample in samples
                    ],
                )
                draft = make_publish_draft(product, analysis)
                conn.execute(
                    """
                    insert into drafts (product_id, title, price, body, warnings, decision, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        draft["title"],
                        draft["price"],
                        draft["body"],
                        json.dumps(draft["warnings"], ensure_ascii=False),
                        draft["decision"],
                        now(),
                    ),
                )
                conn.execute(
                    """
                    insert into publish_queue (product_id, title, price, body, status, source, warnings, created_at, updated_at)
                    values (?, ?, ?, ?, 'ready', 'autopilot', ?, ?, ?)
                    """,
                    (
                        product_id,
                        draft["title"],
                        draft["price"],
                        draft["body"],
                        json.dumps(draft["warnings"], ensure_ascii=False),
                        now(),
                        now(),
                    ),
                )
                conn.execute(
                    """
                    insert into opportunities (
                        keyword, category, cost, min_profit, risk_buffer, sample_prices, sample_count,
                        min_price, median_price, suggested_price, max_purchase_price,
                        expected_profit, viable, decision, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        item["keywords"],
                        item["category"],
                        item["cost"],
                        min_profit,
                        risk_buffer,
                        json.dumps(item["sample_prices"], ensure_ascii=False),
                        len(item["sample_prices"]),
                        analysis.min_price,
                        analysis.median_price,
                        analysis.suggested_price,
                        analysis.max_purchase_price,
                        analysis.expected_profit,
                        analysis.decision,
                        now(),
                    ),
                )
                existing_names.add(product["name"])
                created += 1
        self.send_json({"created": created, **app_state()}, HTTPStatus.CREATED)

    def cleanup_labor_products(self) -> None:
        with connect() as conn:
            product_ids = [
                row["id"]
                for row in conn.execute("select id from products where category = '无物流服务'").fetchall()
            ]
            if product_ids:
                placeholders = ",".join("?" for _ in product_ids)
                conn.execute(f"delete from publish_queue where product_id in ({placeholders})", product_ids)
                conn.execute(f"delete from drafts where product_id in ({placeholders})", product_ids)
                conn.execute(f"delete from orders where product_id in ({placeholders})", product_ids)
                conn.execute(f"delete from market_samples where product_id in ({placeholders})", product_ids)
                conn.execute(f"delete from products where id in ({placeholders})", product_ids)
        self.send_json({"removed": len(product_ids), **app_state()})

    def update_publish_queue_status(self, payload: dict[str, Any]) -> None:
        queue_id = int(payload.get("queue_id", 0))
        status = payload.get("status", "").strip()
        if status not in {"ready", "filled", "published", "skipped"}:
            raise ValueError("发布队列状态不合法")
        with connect() as conn:
            conn.execute(
                "update publish_queue set status = ?, updated_at = ? where id = ?",
                (status, now(), queue_id),
            )
        self.send_json(app_state())


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>闲鱼价差运营台</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #202124;
      --muted: #626a73;
      --line: #d8dee4;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --accent: #0b6bcb;
      --accent-2: #188038;
      --warn: #b45309;
      --bad: #b3261e;
      --soft: #e8f0fe;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      line-height: 1.5;
    }
    header {
      background: #fff;
      border-bottom: 1px solid var(--line);
      padding: 18px 24px;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    header p { margin: 4px 0 0; color: var(--muted); font-size: 14px; }
    main {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 18px;
      padding: 18px;
      max-width: 1440px;
      margin: 0 auto;
    }
    section, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2, h3 { margin: 0 0 12px; font-size: 17px; letter-spacing: 0; }
    h3 { font-size: 15px; }
    label { display: block; color: var(--muted); font-size: 13px; margin: 10px 0 5px; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      background: #fff;
    }
    textarea { min-height: 90px; resize: vertical; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 9px 12px;
      font: inherit;
      color: #fff;
      background: var(--accent);
      cursor: pointer;
      min-height: 38px;
    }
    button.secondary { background: #4b5563; }
    button.good { background: var(--accent-2); }
    button:disabled { opacity: .6; cursor: wait; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .product-list { display: grid; gap: 10px; }
    .compact-list { display: grid; gap: 8px; }
    .compact-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      background: #fbfcfd;
    }
    .compact-item strong { display: block; font-size: 14px; }
    .compact-item span { color: var(--muted); display: block; font-size: 12px; }
    .product-tab {
      text-align: left;
      color: var(--ink);
      background: #fff;
      border: 1px solid var(--line);
      display: block;
      width: 100%;
    }
    .product-tab.active { border-color: var(--accent); background: var(--soft); }
    .product-tab strong { display: block; font-size: 15px; }
    .product-tab span { color: var(--muted); font-size: 13px; }
    .workspace { display: grid; gap: 18px; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-height: 74px;
      background: #fbfcfd;
    }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; margin-top: 5px; font-size: 20px; }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      border-radius: 999px;
      padding: 3px 9px;
      font-size: 13px;
      color: #134e4a;
      background: #ccfbf1;
      margin-right: 6px;
    }
    .badge.warn { color: #7c2d12; background: #ffedd5; }
    .badge.bad { color: #7f1d1d; background: #fee2e2; }
    .notice {
      border-left: 4px solid var(--warn);
      background: #fff7ed;
      padding: 10px 12px;
      margin: 8px 0;
      color: #7c2d12;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      padding: 12px;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 90px;
    }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    .empty { color: var(--muted); padding: 24px; text-align: center; }
    .search-link { color: var(--accent); text-decoration: none; font-size: 14px; }
    .helper-text { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
    @media (max-width: 980px) {
      main, .grid2 { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
  <header>
    <h1>闲鱼价差运营台</h1>
    <p>商品规划、行情采样、定价发布、接单后采购与缺货保护。</p>
  </header>
  <main>
    <aside class="panel">
      <h2>后台自动选品</h2>
      <form id="autopilotForm">
        <div class="row">
          <div>
            <label>本次选品数</label>
            <input name="max_items" type="number" min="1" max="6" value="3">
          </div>
          <div>
            <label>最低利润</label>
            <input name="min_profit" type="number" step="0.01" value="3">
          </div>
        </div>
        <label>风险缓冲</label>
        <input name="risk_buffer" type="number" step="0.01" value="1">
        <div class="actions">
          <button type="submit" class="good">自动选品入队</button>
          <button type="button" id="cleanupLabor" class="secondary">清理服务类候选</button>
        </div>
        <p class="helper-text">系统会从无需人力服务的数字权益候选池里选择商品，生成草稿并加入发布队列。</p>
      </form>
      <hr>
      <h2>已规划商品</h2>
      <div id="productList" class="product-list"></div>
      <hr>
      <h2>机会扫描</h2>
      <form id="opportunityForm">
        <label>类目</label>
        <input name="category" placeholder="虚拟权益、服务">
        <div class="row">
          <div>
            <label>已知成本</label>
            <input name="cost" type="number" step="0.01" value="0">
          </div>
          <div>
            <label>最低利润</label>
            <input name="min_profit" type="number" step="0.01" value="3">
          </div>
        </div>
        <label>风险缓冲</label>
        <input name="risk_buffer" type="number" step="0.01" value="1">
        <label>关键词与价格</label>
        <textarea name="bulk_text" placeholder="会员月卡 | 15元 16元 18元 20元&#10;某权益周卡 | 8 9.5 11 12"></textarea>
        <div class="actions">
          <button type="submit">扫描机会</button>
        </div>
      </form>
      <div id="opportunityList" class="compact-list"></div>
    </aside>

    <div class="workspace" id="workspace">
      <section class="empty">先添加一个商品，然后录入闲鱼搜索结果价格。</section>
    </div>
  </main>

  <script>
    let state = { products: [] };
    let selectedId = null;

    const $ = (selector, root = document) => root.querySelector(selector);
    const money = (value) => Number(value || 0).toFixed(2).replace(/\.00$/, "").replace(/0$/, "");

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "请求失败");
      return data;
    }

    async function refresh() {
      state = await api("/api/state");
      if (!selectedId && state.products.length) selectedId = state.products[0].id;
      if (selectedId && !state.products.some((p) => p.id === selectedId)) selectedId = state.products[0]?.id || null;
      render();
    }

    function selectedProduct() {
      return state.products.find((product) => product.id === selectedId);
    }

    function renderProductList() {
      const list = $("#productList");
      if (!state.products.length) {
        list.innerHTML = '<div class="empty">还没有商品</div>';
        return;
      }
      list.innerHTML = state.products.map((product) => `
        <button class="product-tab ${product.id === selectedId ? "active" : ""}" data-id="${product.id}">
          <strong>${escapeHtml(product.name)}</strong>
          <span>${escapeHtml(product.category || "未分类")} · 建议价 ${money(product.analysis.suggested_price)} 元</span>
        </button>
      `).join("");
      list.querySelectorAll("button").forEach((button) => {
        button.addEventListener("click", () => {
          selectedId = Number(button.dataset.id);
          render();
        });
      });
    }

    function renderOpportunityList() {
      const list = $("#opportunityList");
      if (!state.opportunities?.length) {
        list.innerHTML = '<div class="empty">还没有机会扫描结果</div>';
        return;
      }
      list.innerHTML = state.opportunities.slice(0, 12).map((item) => `
        <div class="compact-item">
          <strong>${escapeHtml(item.keyword)}</strong>
          <span>${item.viable ? "可测试" : "需谨慎"} · 建议 ${money(item.suggested_price)} 元 · 利润 ${money(item.expected_profit)} 元 · 样本 ${item.sample_count}</span>
          <div class="actions">
            <button class="good make-product" data-id="${item.id}">转商品</button>
          </div>
        </div>
      `).join("");
      list.querySelectorAll(".make-product").forEach((button) => {
        button.addEventListener("click", async () => {
          state = await api("/api/products/from-opportunity", {
            method: "POST",
            body: JSON.stringify({ opportunity_id: Number(button.dataset.id) }),
          });
          selectedId = state.products[0]?.id || selectedId;
          render();
        });
      });
    }

    function renderWorkspace() {
      const product = selectedProduct();
      const workspace = $("#workspace");
      const queueHtml = renderPublishQueue(state.publish_queue || []);
      if (!product) {
        workspace.innerHTML = `${queueHtml}<section class="empty">先运行后台自动选品，或从机会扫描转入商品。</section>`;
        return;
      }
      const a = product.analysis;
      const draft = product.draft_preview;
      const query = encodeURIComponent(product.keywords || product.name);
      workspace.innerHTML = `
        ${queueHtml}
        <section>
          <h2>${escapeHtml(product.name)}</h2>
          <p>
            <span class="badge ${a.viable ? "" : "warn"}">${a.viable ? "可测试" : "需谨慎"}</span>
            <span class="badge">样本 ${a.sample_count}</span>
            <a class="search-link" href="https://www.goofish.com/search?q=${query}" target="_blank">打开闲鱼搜索</a>
          </p>
          <div class="actions">
            <button id="copyCollector" class="secondary">复制采样脚本</button>
          </div>
          <p class="helper-text">先打开搜索页，等结果加载后运行采样脚本；脚本只读取当前页面文字并导入价格样本。</p>
          <div class="metrics">
            ${metric("最低价", a.min_price)}
            ${metric("低价区间", a.q1_price)}
            ${metric("中位数", a.median_price)}
            ${metric("去极值均价", a.trimmed_avg)}
            ${metric("建议售价", a.suggested_price)}
          </div>
          <p><strong>决策：</strong>${escapeHtml(a.decision)}</p>
          <p><strong>采购上限：</strong>${money(a.max_purchase_price)} 元；<strong>预估利润：</strong>${money(a.expected_profit)} 元；<strong>加价策略：</strong>主流价 + ${money(a.recommended_markup)} 元。</p>
          ${draft.warnings.map((warning) => `<div class="notice">${escapeHtml(warning)}</div>`).join("")}
        </section>

        <div class="grid2">
          <section>
            <h3>录入行情</h3>
            <form id="sampleForm">
              <label>批量粘贴搜索结果</label>
              <textarea name="bulk_text" placeholder="例：某会员月卡 18元 有效期30天&#10;同类商品 ￥16.5 秒发"></textarea>
              <div class="row">
                <div>
                  <label>单条标题</label>
                  <input name="title" placeholder="可选">
                </div>
                <div>
                  <label>单条价格</label>
                  <input name="price" type="number" step="0.01" placeholder="可选">
                </div>
              </div>
              <div class="actions">
                <button type="submit">保存行情</button>
              </div>
            </form>
          </section>

          <section>
            <h3>发布草稿</h3>
            <p><strong>标题：</strong>${escapeHtml(draft.title)}</p>
            <p><strong>价格：</strong>${money(draft.price)} 元</p>
            <pre>${escapeHtml(draft.body)}</pre>
            <div class="actions">
              <button id="saveDraft" class="good">保存草稿</button>
              <button id="copyPublisher" class="secondary">复制填表脚本</button>
              <a class="search-link" href="https://www.goofish.com" target="_blank">打开闲鱼</a>
            </div>
            <p class="helper-text">进入发布编辑页后运行填表脚本；脚本只填内容，不点击发布。</p>
          </section>
        </div>

        <div class="grid2">
          <section>
            <h3>咨询回复</h3>
            <form id="replyForm">
              <label>买家问题</label>
              <textarea name="question" placeholder="例：有货吗？能不能便宜？怎么用？"></textarea>
              <div class="actions">
                <button type="submit">生成回复</button>
              </div>
            </form>
            <pre id="replyOutput"></pre>
          </section>

          <section>
            <h3>接单履约</h3>
            <form id="orderForm">
              <label>买家备注</label>
              <input name="buyer" placeholder="昵称或订单备注">
              <label>成交价</label>
              <input name="sale_price" type="number" step="0.01" value="${a.suggested_price}">
              <label>最新低价货源</label>
              <textarea name="fresh_sources" placeholder="接单后重新搜索，把候选低价结果粘贴到这里。例：低价月卡 15元 可用"></textarea>
              <div class="actions">
                <button type="submit">判断货源</button>
              </div>
            </form>
            <p class="notice">系统会优先使用最新粘贴的货源，并结合历史样本排序；如果没有价格低于采购上限的候选货源，会生成缺货处理建议。</p>
          </section>
        </div>

        <section>
          <h3>行情样本</h3>
          ${renderSamples(product.samples)}
        </section>

        <section>
          <h3>订单记录</h3>
          ${renderOrders(product.orders)}
        </section>
      `;
      bindWorkspaceForms(product.id);
    }

    function metric(label, value) {
      return `<div class="metric"><span>${label}</span><strong>${money(value)} 元</strong></div>`;
    }

    function renderSamples(samples) {
      if (!samples.length) return '<div class="empty">还没有行情样本</div>';
      return `
        <table>
          <thead><tr><th>价格</th><th>标题</th><th>备注</th></tr></thead>
          <tbody>${samples.map((sample) => `
            <tr><td>${money(sample.price)} 元</td><td>${escapeHtml(sample.title)}</td><td>${escapeHtml(sample.note || "")}</td></tr>
          `).join("")}</tbody>
        </table>
      `;
    }

    function renderOrders(orders) {
      if (!orders.length) return '<div class="empty">还没有订单</div>';
      return `
        <table>
          <thead><tr><th>状态</th><th>成交价</th><th>采购上限</th><th>处理建议</th></tr></thead>
          <tbody>${orders.map((order) => `
            <tr>
              <td><span class="badge ${order.status === "out_of_stock" ? "bad" : ""}">${escapeHtml(order.status)}</span></td>
              <td>${money(order.sale_price)} 元</td>
              <td>${money(order.max_purchase_price)} 元</td>
              <td>${escapeHtml(order.reply)}</td>
            </tr>
          `).join("")}</tbody>
        </table>
      `;
    }

    function renderPublishQueue(queue) {
      if (!queue.length) {
        return '<section><h2>发布队列</h2><div class="empty">还没有待发布商品。可以运行后台自动选品。</div></section>';
      }
      return `
        <section>
          <h2>发布队列</h2>
          <table>
            <thead><tr><th>状态</th><th>商品</th><th>价格</th><th>操作</th></tr></thead>
            <tbody>${queue.map((item) => `
              <tr>
                <td><span class="badge ${item.status === "skipped" ? "bad" : ""}">${escapeHtml(item.status)}</span></td>
                <td>
                  <strong>${escapeHtml(item.product_name)}</strong><br>
                  <span class="helper-text">${escapeHtml(item.title)}</span>
                </td>
                <td>${money(item.price)} 元</td>
                <td>
                  <div class="actions">
                    <button class="secondary copy-queue-publisher" data-product-id="${item.product_id}">复制填表</button>
                    <button class="good queue-status" data-id="${item.id}" data-status="filled">标记已填</button>
                    <button class="secondary queue-status" data-id="${item.id}" data-status="skipped">跳过</button>
                  </div>
                </td>
              </tr>
            `).join("")}</tbody>
          </table>
        </section>
      `;
    }

    function bindWorkspaceForms(productId) {
      $("#sampleForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        await submitForm(event.currentTarget, "/api/market-samples", { product_id: productId });
      });
      $("#copyCollector").addEventListener("click", async () => {
        const script = `javascript:(()=>{fetch("http://127.0.0.1:8765/collector.js?product_id=${productId}").then(r=>r.text()).then(code=>eval(code));})()`;
        await navigator.clipboard.writeText(script);
        alert("采样脚本已复制。打开闲鱼搜索页后，把它粘贴到地址栏或保存成书签运行。");
      });
      $("#saveDraft").addEventListener("click", async () => {
        await api("/api/drafts", { method: "POST", body: JSON.stringify({ product_id: productId }) });
        await refresh();
      });
      $("#copyPublisher").addEventListener("click", async () => {
        const script = `javascript:(()=>{fetch("http://127.0.0.1:8765/publisher.js?product_id=${productId}").then(r=>r.text()).then(code=>eval(code));})()`;
        await navigator.clipboard.writeText(script);
        alert("填表脚本已复制。进入闲鱼发布编辑页后，把它粘贴到地址栏或保存成书签运行。");
      });
      $("#replyForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const payload = Object.fromEntries(new FormData(form).entries());
        payload.product_id = productId;
        const data = await api("/api/replies", { method: "POST", body: JSON.stringify(payload) });
        $("#replyOutput").textContent = data.reply;
      });
      $("#orderForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        await submitForm(event.currentTarget, "/api/orders", { product_id: productId });
      });
    }

    async function submitForm(form, path, extra = {}) {
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const payload = { ...Object.fromEntries(new FormData(form).entries()), ...extra };
        state = await api(path, { method: "POST", body: JSON.stringify(payload) });
        form.reset();
        render();
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    }

    function render() {
      renderProductList();
      renderOpportunityList();
      renderWorkspace();
      bindQueueActions();
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    $("#opportunityForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await submitForm(event.currentTarget, "/api/opportunities");
    });

    $("#autopilotForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await submitForm(event.currentTarget, "/api/autopilot/run");
      selectedId = state.products[0]?.id || selectedId;
      render();
    });

    $("#cleanupLabor").addEventListener("click", async () => {
      state = await api("/api/autopilot/cleanup-labor", { method: "POST", body: "{}" });
      selectedId = state.products[0]?.id || null;
      render();
      alert(`已清理 ${state.removed || 0} 个服务类候选。`);
    });

    function bindQueueActions() {
      document.querySelectorAll(".copy-queue-publisher").forEach((button) => {
        button.addEventListener("click", async () => {
          const productId = Number(button.dataset.productId);
          const script = `javascript:(()=>{fetch("http://127.0.0.1:8765/publisher.js?product_id=${productId}").then(r=>r.text()).then(code=>eval(code));})()`;
          await navigator.clipboard.writeText(script);
          alert("发布队列填表脚本已复制。进入闲鱼发布编辑页后运行，人工检查后再发布。");
        });
      });
      document.querySelectorAll(".queue-status").forEach((button) => {
        button.addEventListener("click", async () => {
          state = await api("/api/publish-queue/status", {
            method: "POST",
            body: JSON.stringify({ queue_id: Number(button.dataset.id), status: button.dataset.status }),
          });
          render();
        });
      });
    }

    refresh().catch((error) => {
      $("#workspace").innerHTML = `<section class="empty">${escapeHtml(error.message)}</section>`;
    });
  </script>
</body>
</html>
"""


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"闲鱼价差运营台已启动：http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
