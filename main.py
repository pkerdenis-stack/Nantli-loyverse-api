import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query

import csv
import io

from fastapi.responses import StreamingResponse

app = FastAPI(
    title="Nantli Loyverse API",
    version="2.0"
)

LOYVERSE_TOKEN = os.getenv("LOYVERSE_TOKEN")
LOYVERSE_BASE_URL = "https://api.loyverse.com/v1.0"

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
        "Authorization": f"Bearer {LOYVERSE_TOKEN}"
    }


async def loyverse_get(endpoint: str, params=None):
    url = f"{LOYVERSE_BASE_URL}/{endpoint}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            headers=loyverse_headers(),
            params=params
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


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


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "Nantli Loyverse API",
        "version": "2.0",
        "available_endpoints": [
            "/health",
            "/receipts",
            "/receipts?date=2026-08-12",
            "/receipts?from_date=2026-08-12&to_date=2026-08-18",
            "/sales-summary?date=2026-08-12",
            "/items"
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