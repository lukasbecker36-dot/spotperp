"""Test-wide setup.

The screener / safety fixtures were calibrated on MEXC's spot fees (0% maker, 0.05% taker): edges and
floors in them sit just either side of those costs. The spot venue is KuCoin now (0.1% / 0.1%), so pin
the fees the fixtures were written for. Set before config is imported; test_kucoin checks KuCoin's own
defaults separately.
"""
import os

os.environ.setdefault("SPOT_MAKER_FEE", "0.0")
os.environ.setdefault("SPOT_TAKER_FEE", "0.0005")
