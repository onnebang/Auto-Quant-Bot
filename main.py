import yfinance as yf
import FinanceDataReader as fdr
import pandas as pd
import requests
from datetime import datetime
import time
import argparse
import os

# ==========================================
# 1. 디스코드 웹훅 URL (GitHub Secrets에서 불러오기 - 보안 강화)
# ==========================================
# 깃헙에 코드를 올릴 때 URL이 노출되면 안 되므로 환경변수(os.getenv)를 사용합니다.
WEBHOOK_URLS = {
    "KR_DCA": os.getenv("WEBHOOK_KR_DCA", "https://discord.com/api/webhooks/1481933965328650460/p87tLK2MS9mDQxyFik8Gya66og0L876typVoo02ZcE4m7iFlDEV65ozSntKhaXCIt-Vk"),
    "KR_DANTA": os.getenv("WEBHOOK_KR_DANTA", "https://discord.com/api/webhooks/1481933858751647767/Faaw3bHt0O940L1Wl9j8CgMu-abDE3gv2wd2WiySfY5_l9cBS5kpAjDoouF0B8KWU6NB"),
    "US_DCA": os.getenv("WEBHOOK_US_DCA", "https://discord.com/api/webhooks/1481934209940721786/e74rvcilcyZ6B8V-5JLEmnCpZI661At1ZcObYm7vXDWCjpkNdaHBls1MEou3QwxM520z"),
    "US_DANTA": os.getenv("WEBHOOK_US_DANTA", "https://discord.com/api/webhooks/1481934290655641711/j_APkbgdA5YW5IwjoCg10rxfZp5rAk20dXLmlp6wLoflpM559_B1m-21BZsDqdHiYUpr"),
    "JP_DCA": os.getenv("WEBHOOK_JP_DCA", "https://discord.com/api/webhooks/1481934209940721786/e74rvcilcyZ6B8V-5JLEmnCpZI661At1ZcObYm7vXDWCjpkNdaHBls1MEou3QwxM520z"),
    "JP_DANTA": os.getenv("WEBHOOK_JP_DANTA", "https://discord.com/api/webhooks/1481934290655641711/j_APkbgdA5YW5IwjoCg10rxfZp5rAk20dXLmlp6wLoflpM559_B1m-21BZsDqdHiYUpr")
}

def send_discord_msg(webhook_url, title, description, color):
    if not webhook_url or not webhook_url.startswith("http"): return
    data = {"embeds": [{"title": title, "description": description, "color": color}]}
    requests.post(webhook_url, json=data)

# ==========================================
# 2. 시장별 유니버스 구성 (한국, 미국, 일본)
# ==========================================
def get_universe(market):
    if market == "kr":
        df_kr = fdr.StockListing('KOSPI')
        kr_mc_80 = df_kr.sort_values('Marcap', ascending=False).head(80)
        kr_vol_20 = df_kr[~df_kr['Code'].isin(kr_mc_80['Code'])].sort_values('Volume', ascending=False).head(20)
        kr_combined = pd.concat([kr_mc_80, kr_vol_20])
        return [f"{code}.KS" for code in kr_combined['Code'].tolist()]
    
    elif market == "us":
        df_us = fdr.StockListing('S&P500')
        return df_us['Symbol'].head(100).tolist()
    
    elif market == "jp":
        # 일본 시가총액/거래대금 상위 30개 우량주 하드코딩 (안정성 확보)
        # 도요타(7203), 소니(6758), 미쓰비시UFJ(8306), 도쿄일렉트론(8035) 등
        jp_codes = ["7203", "6758", "8306", "8035", "9984", "6861", "9432", "8058", "4063", "8316", 
                    "7974", "8031", "6920", "7267", "6501", "4568", "8001", "8766", "8002", "3382",
                    "6098", "6702", "4502", "7741", "4519", "6981", "7269", "4661", "8802", "9433"]
        return [f"{code}.T" for code in jp_codes]
    return []

# ==========================================
# 3. 보조지표 및 심층 분석 (이전 코드와 동일)
# ==========================================
def calculate_indicators(df):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    df['BB_Mid'] = df['Close'].rolling(window=20).mean()
    df['BB_Std'] = df['Close'].rolling(window=20).std()
    df['BB_Lower'] = df['BB_Mid'] - (df['BB_Std'] * 2)
    
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
    return df

def get_deep_analysis(ticker, hist_df):
    stock = yf.Ticker(ticker)
    info = stock.info
    
    prev_close = hist_df['Close'].iloc[-2]
    current = hist_df['Close'].iloc[-1]
    change_pct = round(((current - prev_close) / prev_close) * 100, 2)
    change_icon = "🔺" if change_pct > 0 else "🔻"
    
    vol_current = hist_df['Volume'].iloc[-1]
    vol_avg_5d = hist_df['Volume'].tail(5).mean()
    vol_surge = round((vol_current / vol_avg_5d) * 100, 1) if vol_avg_5d > 0 else 0
    
    per = info.get('trailingPE', 'N/A')
    pbr = info.get('priceToBook', 'N/A')
    roe = info.get('returnOnEquity', 'N/A')
    div = info.get('dividendYield', 'N/A')
    
    if roe != 'N/A': roe = f"{round(roe * 100, 2)}%"
    if div != 'N/A': div = f"{round(div * 100, 2)}%"
    if per != 'N/A': per = round(per, 2)
    if pbr != 'N/A': pbr = round(pbr, 2)
    
    report = (f"**주가 변동:** ${current} ({change_icon}{change_pct}%)\n"
              f"**수급 동향:** 거래량 {vol_surge}% (5일 평균대비)\n"
              f"**가치 평가:** PER {per} / PBR {pbr}\n"
              f"**수익/배당:** ROE {roe} / 배당 {div}")
    return report, change_pct

# ==========================================
# 4. 스캐닝 엔진 (전략에 따라 분기)
# ==========================================
def run_bot(market, mode):
    tickers = get_universe(market)
    market_name = {"kr": "한국", "us": "미국", "jp": "일본"}[market]
    dca_webhook = WEBHOOK_URLS[f"{market.upper()}_DCA"]
    danta_webhook = WEBHOOK_URLS[f"{market.upper()}_DANTA"]
    
    print(f"\n⚙️ [{market_name}] 시장 [{mode.upper()}] 전략 스캔 시작...")
    
    for i, ticker in enumerate(tickers):
        try:
            if (i+1) % 20 == 0: print(f"   진행률: {i+1}/{len(tickers)}")
            stock = yf.Ticker(ticker)
            
            # [DCA 모드]
            if mode == "dca":
                hist = stock.history(period="60d", interval="1d")
                if len(hist) >= 26:
                    df = calculate_indicators(hist)
                    rsi = round(df['RSI'].iloc[-1], 2)
                    bb_lower = round(df['BB_Lower'].iloc[-1], 2)
                    current_price = df['Close'].iloc[-1]
                    
                    if rsi < 30 and current_price <= bb_lower:
                        deep_report, _ = get_deep_analysis(ticker, df)
                        msg = f"🩸 **과매도 바닥 구간 진입 (RSI: {rsi})**\n{deep_report}"
                        send_discord_msg(dca_webhook, f"🚨 {ticker} DCA 타점", msg, 16711680)

            # [단타 모드]
            elif mode == "danta":
                hist = stock.history(period="15d", interval="1h")
                if len(hist) >= 26:
                    df = calculate_indicators(hist)
                    rsi_h = round(df['RSI'].iloc[-1], 2)
                    macd_curr = df['MACD_Hist'].iloc[-1]
                    macd_prev = df['MACD_Hist'].iloc[-2]
                    
                    if rsi_h < 40 and (macd_prev < 0 and macd_curr > 0):
                        deep_report, change_pct = get_deep_analysis(ticker, df)
                        color = 16711680 if change_pct > 0 else 255
                        msg = f"📈 **단기 반등 골든크로스 (60분봉 RSI: {rsi_h})**\n{deep_report}"
                        send_discord_msg(danta_webhook, f"⚡ {ticker} 단기 스윙 타점", msg, color)
                        
            time.sleep(0.1)
        except:
            pass

# ==========================================
# 5. 실행부 (명령어 인수 처리)
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    parser.add_argument('--mode', type=str, required=True, choices=['dca', 'danta'])
    args = parser.parse_args()
    
    run_bot(args.market, args.mode)
