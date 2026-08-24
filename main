import os

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Nantli Loyverse API")

LOYVERSE_TOKEN = os.getenv("LOYVERSE_TOKEN")
LOYVERSE_BASE_URL = "https://api.loyverse.com/v1.0"


def loyverse_headers():
    if not LOYVERSE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="LOYVERSE_TOKEN is not configured"
        )

    return {
        "Authorization": f"Bearer {LOYVERSE_TOKEN}"
    }


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "Nantli Loyverse API"
    }


@app.get("/receipts")
async def get_receipts():
    url = f"{LOYVERSE_BASE_URL}/receipts"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            headers=loyverse_headers()
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


@app.get("/items")
async def get_items():
    url = f"{LOYVERSE_BASE_URL}/items"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            headers=loyverse_headers()
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()