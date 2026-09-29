import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

import csv
import io
import requests

from typing import Optional
from fastapi import HTTPException

from fastapi.responses import StreamingResponse

app = FastAPI(
    title="Nantli Loyverse API",
    version="2.1"
)

LOYVERSE_TOKEN = os.getenv("LOYVERSE_TOKEN")
INVENTORY_API_KEY = os.getenv("INVENTORY_API_KEY")
LOYVERSE_BASE_URL = "https://api.loyverse.com/v1.0"


class ReceivedItem(BaseModel):
    sku: str = Field(min_length=1)
    quantity_received: float = Field(gt=0)


class InventoryPreviewRequest(BaseModel):
    store_name: str = Field(default="Barsito", min_length=1)
    items: list[ReceivedItem] = Field(min_length=1)


class InventoryCommitItem(ReceivedItem):
    expected_current_stock: float


class InventoryCommitRequest(BaseModel):
    reference: str = Field(min_length=1)
    store_name: str = Field(default="Barsito", min_length=1)
    items: list[InventoryCommitItem] = Field(min_length=1)

@app.get("/shifts")
async def get_shifts(
    from_date: str | None = Query(
        default=None,
        description="Start date in YYYY-MM-DD format"
    ),
    to_date: str | None = Query(
        default=None,
        description="End date in YYYY-MM-DD format"
    ),
):
    params = {}

    try:
        if from_date:
            start = datetime.strptime(from_date, "%Y-%m-%d")

            params["created_at_min"] = (
                start.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        if to_date:
            end = (
                datetime.strptime(to_date, "%Y-%m-%d")
                + timedelta(days=1)
            )

            params["created_at_max"] = (
                end.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Dates must use YYYY-MM-DD format"
        )

    data = await loyverse_get(
        "shifts",
        params=params
    )

    shifts = data.get("shifts", [])

    return {
        "count": len(shifts),
        "filters": {
            "from_date": from_date,
            "to_date": to_date
        },
        "shifts": shifts
    }

def loyverse_headers():
    if not LOYVERSE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="LOYVERSE_TOKEN is not configured"
        )

    return {
        "Authorization": f"Bearer {LOYVERSE_TOKEN}",
        "Content-Type": "application/json",
    }


async def loyverse_request(
    method: Literal["GET", "POST"],
    endpoint: str,
    *,
    params=None,
    json=None,
):
    url = f"{LOYVERSE_BASE_URL}/{endpoint}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method,
            url,
            headers=loyverse_headers(),
            params=params,
            json=json,
        )

    if not response.is_success:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


async def loyverse_get(endpoint: str, params=None):
    return await loyverse_request("GET", endpoint, params=params)


async def loyverse_post(endpoint: str, json):
    return await loyverse_request("POST", endpoint, json=json)


def require_inventory_key(x_inventory_key: str | None):
    if not INVENTORY_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="INVENTORY_API_KEY is not configured"
        )

    if not x_inventory_key or not secrets.compare_digest(
        x_inventory_key,
        INVENTORY_API_KEY,
    ):
        raise HTTPException(status_code=401, detail="Invalid inventory key")


async def get_all(endpoint: str, collection: str, params=None):
    params = dict(params or {})
    params["limit"] = 250
    records = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor
        else:
            params.pop("cursor", None)

        data = await loyverse_get(endpoint, params=params)
        records.extend(data.get(collection, []))
        cursor = data.get("cursor")

        if not cursor:
            return records


async def resolve_store(store_name: str):
    stores = await get_all("stores", "stores")
    matches = [
        store for store in stores
        if store.get("name", "").casefold() == store_name.casefold()
    ]

    if len(matches) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected one store named {store_name!r}, "
                f"found {len(matches)}"
            )
        )

    return matches[0]


async def resolve_skus(skus: list[str]):
    if len(skus) != len(set(skus)):
        raise HTTPException(status_code=400, detail="Duplicate SKU in request")

    items = await get_all("items", "items")
    by_sku = {}

    for item in items:
        for variant in item.get("variants", []):
            sku = variant.get("sku")
            if sku in skus:
                if sku in by_sku:
                    raise HTTPException(
                        status_code=409,
                        detail=f"SKU {sku!r} is not unique in Loyverse"
                    )
                by_sku[sku] = {
                    "item_name": item.get("item_name"),
                    "variant_id": variant.get("variant_id"),
                }

    missing = [sku for sku in skus if sku not in by_sku]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"message": "SKUs not found", "skus": missing}
        )

    return by_sku


async def inventory_by_variant(store_id: str, variant_ids: list[str]):
    data = await loyverse_get(
        "inventory",
        params={"store_id": store_id, "variant_ids": ",".join(variant_ids)},
    )
    return {
        level["variant_id"]: float(level.get("in_stock") or 0)
        for level in data.get("inventory_levels", [])
    }


async def build_inventory_preview(request: InventoryPreviewRequest):
    store = await resolve_store(request.store_name)
    resolved = await resolve_skus([item.sku for item in request.items])
    variant_ids = [resolved[item.sku]["variant_id"] for item in request.items]
    current = await inventory_by_variant(store["id"], variant_ids)

    lines = []
    for received in request.items:
        match = resolved[received.sku]
        current_stock = current.get(match["variant_id"], 0.0)
        lines.append({
            "sku": received.sku,
            "item_name": match["item_name"],
            "variant_id": match["variant_id"],
            "quantity_received": received.quantity_received,
            "current_stock": current_stock,
            "proposed_stock": current_stock + received.quantity_received,
        })

    return store, lines


async def get_all_receipts(params=None):
    """
    Automatically follows Loyverse pagination and returns all matching receipts.
    """
    params = dict(params or {})
    params["limit"] = 250

    all_receipts = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor
        else:
            params.pop("cursor", None)

        data = await loyverse_get("receipts", params=params)

        all_receipts.extend(data.get("receipts", []))

        cursor = data.get("cursor")

        if not cursor:
            break

    return all_receipts

@app.get("/inventory")
def get_inventory(
    store_ids: Optional[str] = None,
    variant_ids: Optional[str] = None,
):
    """
    Read current Loyverse inventory levels.
    This endpoint is read-only and never modifies stock.
    """

    token = os.getenv("LOYVERSE_TOKEN")

    if not token:
        raise HTTPException(
            status_code=500,
            detail="LOYVERSE_TOKEN is not configured",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    params = {"limit": 250}

    if store_ids:
        params["store_ids"] = store_ids

    if variant_ids:
        params["variant_ids"] = variant_ids

    inventory_levels = []

    while True:
        response = requests.get(
            "https://api.loyverse.com/v1.0/inventory",
            headers=headers,
            params=params,
            timeout=30,
        )

        if response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail=(
                    "The Loyverse token does not have INVENTORY_READ permission. "
                    "Reauthorize the token with INVENTORY_READ enabled."
                ),
            )

        if not response.ok:
            raise HTTPException(
                status_code=response.status_code,
                detail={
                    "message": "Loyverse inventory request failed",
                    "loyverse_response": response.text,
                },
            )

        payload = response.json()
        inventory_levels.extend(payload.get("inventory_levels", []))

        cursor = payload.get("cursor")

        if not cursor:
            break

        params["cursor"] = cursor

    return {
        "count": len(inventory_levels),
        "inventory_levels": inventory_levels,
    }

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "Nantli Loyverse API",
        "version": "2.1",
        "available_endpoints": [
            "/health",
            "/receipts",
            "/receipts?date=2026-08-12",
            "/receipts?from_date=2026-08-12&to_date=2026-08-18",
            "/sales-summary?date=2026-08-12",
            "/items",
            "/shifts?from_date=2026-08-17&to_date=2026-08-23",
            "/inventory/receive/preview",
            "/inventory/receive",
        ]
    }


@app.get("/health")
async def health():
    """
    Confirms both Render and the Loyverse connection are working.
    """
    try:
        await loyverse_get("receipts", params={"limit": 1})

        return {
            "status": "healthy",
            "render": "online",
            "loyverse": "connected"
        }

    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Loyverse connection failed: {str(exc)}"
        )


@app.get("/receipts")
async def get_receipts(
    date: str | None = Query(
        default=None,
        description="Single date in YYYY-MM-DD format"
    ),
    from_date: str | None = Query(
        default=None,
        description="Start date in YYYY-MM-DD format"
    ),
    to_date: str | None = Query(
        default=None,
        description="End date in YYYY-MM-DD format"
    ),
):
    params = {}

    try:
        if date:
            start = datetime.strptime(date, "%Y-%m-%d")
            end = start + timedelta(days=1)

            params["created_at_min"] = (
                start.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

            params["created_at_max"] = (
                end.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        else:
            if from_date:
                start = datetime.strptime(from_date, "%Y-%m-%d")

                params["created_at_min"] = (
                    start.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            if to_date:
                end = (
                    datetime.strptime(to_date, "%Y-%m-%d")
                    + timedelta(days=1)
                )

                params["created_at_max"] = (
                    end.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Dates must use YYYY-MM-DD format"
        )

    receipts = await get_all_receipts(params)

    return {
        "count": len(receipts),
        "filters": {
            "date": date,
            "from_date": from_date,
            "to_date": to_date
        },
        "receipts": receipts
    }


@app.get("/sales-summary")
async def sales_summary(
    date: str = Query(
        ...,
        description="Date in YYYY-MM-DD format"
    )
):
    try:
        start = datetime.strptime(date, "%Y-%m-%d")
        end = start + timedelta(days=1)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Date must use YYYY-MM-DD format"
        )

    params = {
        "created_at_min": (
            start.replace(tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "created_at_max": (
            end.replace(tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    }

    receipts = await get_all_receipts(params)

    sales_receipts = [
        receipt
        for receipt in receipts
        if receipt.get("receipt_type") == "SALE"
        and not receipt.get("cancelled_at")
    ]

    refunds = [
        receipt
        for receipt in receipts
        if receipt.get("receipt_type") == "REFUND"
    ]

    gross_sales = sum(
        float(receipt.get("total_money") or 0)
        for receipt in sales_receipts
    )

    refunds_total = sum(
        abs(float(receipt.get("total_money") or 0))
        for receipt in refunds
    )

    net_sales = gross_sales - refunds_total

    items_sold = 0

    for receipt in sales_receipts:
        for item in receipt.get("line_items", []):
            items_sold += float(item.get("quantity") or 0)

    return {
        "date": date,
        "receipt_count": len(sales_receipts),
        "refund_count": len(refunds),
        "items_sold": items_sold,
        "gross_sales": round(gross_sales, 2),
        "refunds": round(refunds_total, 2),
        "net_sales": round(net_sales, 2)
    }


@app.get("/items")
async def get_items():
    return await loyverse_get(
        "items",
        params={"limit": 250}
    )


@app.post("/inventory/receive/preview")
async def preview_inventory_receipt(
    request: InventoryPreviewRequest,
    x_inventory_key: str | None = Header(default=None),
):
    """Preview a delivery without changing stock."""
    require_inventory_key(x_inventory_key)
    store, lines = await build_inventory_preview(request)

    return {
        "committed": False,
        "store": {"id": store["id"], "name": store["name"]},
        "total_units_received": sum(
            line["quantity_received"] for line in lines
        ),
        "items": lines,
    }


@app.post("/inventory/receive")
async def commit_inventory_receipt(
    request: InventoryCommitRequest,
    x_inventory_key: str | None = Header(default=None),
):
    """Add received quantities after verifying previewed stock is unchanged."""
    require_inventory_key(x_inventory_key)
    preview_request = InventoryPreviewRequest(
        store_name=request.store_name,
        items=[
            ReceivedItem(
                sku=item.sku,
                quantity_received=item.quantity_received,
            )
            for item in request.items
        ],
    )
    store, lines = await build_inventory_preview(preview_request)
    expected_by_sku = {
        item.sku: item.expected_current_stock for item in request.items
    }
    conflicts = [
        {
            "sku": line["sku"],
            "expected_current_stock": expected_by_sku[line["sku"]],
            "actual_current_stock": line["current_stock"],
        }
        for line in lines
        if line["current_stock"] != expected_by_sku[line["sku"]]
    ]

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Stock changed after preview. Preview again.",
                "conflicts": conflicts,
            },
        )

    result = await loyverse_post(
        "inventory",
        json={
            "inventory_levels": [
                {
                    "variant_id": line["variant_id"],
                    "store_id": store["id"],
                    "stock_after": line["proposed_stock"],
                }
                for line in lines
            ]
        },
    )

    return {
        "committed": True,
        "reference": request.reference,
        "store": {"id": store["id"], "name": store["name"]},
        "total_units_received": sum(
            line["quantity_received"] for line in lines
        ),
        "items": lines,
        "loyverse": result,
    }

@app.get("/receipts.csv")
async def export_receipts_csv(
    date: str | None = Query(
        default=None,
        description="Single date in YYYY-MM-DD format"
    ),
    from_date: str | None = Query(
        default=None,
        description="Start date in YYYY-MM-DD format"
    ),
    to_date: str | None = Query(
        default=None,
        description="End date in YYYY-MM-DD format"
    ),
):
    params = {}

    try:
        if date:
            start = datetime.strptime(date, "%Y-%m-%d")
            end = start + timedelta(days=1)

            params["created_at_min"] = (
                start.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

            params["created_at_max"] = (
                end.replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        else:
            if from_date:
                start = datetime.strptime(
                    from_date,
                    "%Y-%m-%d"
                )

                params["created_at_min"] = (
                    start.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            if to_date:
                end = (
                    datetime.strptime(
                        to_date,
                        "%Y-%m-%d"
                    )
                    + timedelta(days=1)
                )

                params["created_at_max"] = (
                    end.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Dates must use YYYY-MM-DD format"
        )

    receipts = await get_all_receipts(params)

    output = io.StringIO()

    fieldnames = [
        "Date",
        "Receipt number",
        "Receipt type",
        "Gross sales",
        "Discounts",
        "Net sales",
        "Taxes",
        "Total collected",
        "Cost of goods",
        "Gross profit",
        "Payment type",
        "Description",
        "Status",
    ]

    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames
    )

    writer.writeheader()

    for receipt in receipts:

        gross_sales = sum(
            float(item.get("gross_total_money") or 0)
            for item in receipt.get("line_items", [])
        )

        discounts = float(
            receipt.get("total_discount") or 0
        )

        net_sales = float(
            receipt.get("total_money") or 0
        )

        taxes = float(
            receipt.get("total_tax") or 0
        )

        cost_of_goods = sum(
            float(item.get("cost_total") or 0)
            for item in receipt.get("line_items", [])
        )

        gross_profit = (
            net_sales - cost_of_goods
        )

        payment_types = ", ".join(
            payment.get("name", "")
            for payment in receipt.get("payments", [])
        )

        descriptions = []

        for item in receipt.get("line_items", []):
            quantity = item.get("quantity", 0)
            item_name = item.get(
                "item_name",
                "Unknown item"
            )

            descriptions.append(
                f"{quantity} x {item_name}"
            )

        description = ", ".join(descriptions)

        status = (
            "Cancelled"
            if receipt.get("cancelled_at")
            else "Closed"
        )

        receipt_date = receipt.get(
            "receipt_date",
            ""
        )

        writer.writerow({
            "Date": receipt_date,
            "Receipt number": receipt.get(
                "receipt_number",
                ""
            ),
            "Receipt type": receipt.get(
                "receipt_type",
                ""
            ),
            "Gross sales": gross_sales,
            "Discounts": discounts,
            "Net sales": net_sales,
            "Taxes": taxes,
            "Total collected": net_sales,
            "Cost of goods": cost_of_goods,
            "Gross profit": gross_profit,
            "Payment type": payment_types,
            "Description": description,
            "Status": status,
        })

    output.seek(0)

    filename = "nantli_receipts.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )
