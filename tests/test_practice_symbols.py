from app.services.practice_symbols import normalize_perpetual_symbols


def test_symbol_catalog_filters_active_binance_usdt_perpetuals() -> None:
    data = {"symbols": [
        {"symbol":"BTCUSDT","status":"TRADING","quoteAsset":"USDT","contractType":"PERPETUAL"},
        {"symbol":"SOLUSDT","status":"TRADING","quoteAsset":"USDT","contractType":"PERPETUAL"},
        {"symbol":"ETHUSDT","status":"BREAK","quoteAsset":"USDT","contractType":"PERPETUAL"},
        {"symbol":"DOGEUSDT","status":"TRADING","quoteAsset":"USDT","contractType":"CURRENT_QUARTER"},
        {"symbol":"BTCUSDC","status":"TRADING","quoteAsset":"USDC","contractType":"PERPETUAL"},
    ]}
    assert normalize_perpetual_symbols(data)==["BTCUSDT","SOLUSDT"]


def test_symbol_catalog_handles_okx_fallback() -> None:
    data={"source":"OKX_FALLBACK","symbols":[
        {"instId":"BTC-USDT-SWAP","state":"live","settleCcy":"USDT"},
        {"instId":"DOGE-USDT-SWAP","state":"live","settleCcy":"USDT"},
        {"instId":"ETH-USDT-SWAP","state":"suspend","settleCcy":"USDT"},
        {"instId":"BTC-USDC-SWAP","state":"live","settleCcy":"USDC"},
    ]}
    assert normalize_perpetual_symbols(data)==["BTCUSDT","DOGEUSDT"]
