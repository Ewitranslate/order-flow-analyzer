from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.config.settings import Settings, get_settings
from src.features.delta import calculate_delta


def load_trades_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["price"] = df["price"].astype("float64")
    df["volume"] = df["volume"].astype("float64")
    df["side"] = df["side"].astype("string")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    # Vectorized delta: buy -> +volume, sell -> -volume
    df["delta"] = df["volume"].where(df["side"].str.lower() == "buy", -df["volume"])
    df["cum_delta"] = df["delta"].cumsum()
    return df


def build_figure(df: pd.DataFrame, *, title: str = "Order Flow") -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.5, 0.2, 0.3],
        subplot_titles=("Price", "Delta", "Cumulative Delta"),
    )

    x = pd.to_datetime(df["timestamp"], unit="ms")

    fig.add_trace(
        go.Scatter(x=x, y=df["price"], mode="lines", name="Price"),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Bar(x=x, y=df["delta"], name="Delta", marker_color="rgba(120,120,255,0.6)"),
        row=2,
        col=1,
    )

    fig.add_trace(
        go.Scatter(x=x, y=df["cum_delta"], mode="lines", name="Cum Delta"),
        row=3,
        col=1,
    )

    fig.update_layout(
        title=title,
        template="plotly_white",
        height=900,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=60, r=30, t=60, b=40),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Delta", row=2, col=1)
    fig.update_yaxes(title_text="Cum Δ", row=3, col=1)
    return fig


def plot_symbol(symbol: str, *, settings: Optional[Settings] = None) -> go.Figure:
    settings = settings or get_settings()
    path = settings.data_path / f"{symbol.lower()}_trades.csv"
    df = load_trades_csv(path)
    df = add_features(df)
    return build_figure(df, title=f"{symbol.upper()} — Price / Delta / Cum Delta")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="btcusdt", help="e.g. btcusdt")
    p.add_argument("--show", action="store_true", help="open interactive window")
    p.add_argument("--out", default="", help="output html path (optional)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    fig = plot_symbol(args.symbol)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        fig.write_html(args.out)
    if args.show or not args.out:
        fig.show()


if __name__ == "__main__":
    main()

