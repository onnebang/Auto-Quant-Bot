import yfinance as yf
import FinanceDataReader as fdr
import pandas as pd
import requests
from datetime import datetime, timedelta
import time
import argparse
import os
import json
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. 환경 변수 및 디스코드 웹훅 설정
# ==========================================
WEBHOOK_URLS = {
    "KR_DCA": os.getenv("WEBHOOK_KR_DCA"),
    "KR_DANTA": os.getenv("WEBHOOK_KR_DANTA"),
    "US_DCA": os.getenv("WEBHOOK_US_DCA"),
    "US_DANTA": os.getenv("WEBHOOK_US_DANTA"),
    "JP_DCA": os.getenv("WEBHOOK_JP_DCA"),
    "JP_DANTA": os.getenv("WEBHOOK_JP_DANTA")
}

def send_discord_msg(webhook_url, title, description, color, fields=[]):
    if not webhook_url or not webhook_url.startswith("http"): return
    now_kst = datetime.utcnow() + timedelta(hours=9)
    data = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"🤖 펀드매니저 봇 | 분석 시간: {now_kst.strftime('%Y-%m-%d %H:%M')}"},
            "thumbnail": {"url": "https://cdn-icons-png.flaticon.com/512/2422/2422796.png"}
        }]
    }
    requests.post(webhook_url, json=data)

def init_gsheets():
    try:
        json_creds = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
        if not json_creds: return None
        creds_dict = json.loads(json_creds)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        return client.open("QuantBot_Fund").worksheet("Portfolio")
    except Exception as e:
        print(f"❌ 구글 시트 연동 실패: {e}")
        return None

def get_universe_dict(market):
    ticker_dict = {}
    if market == "kr":
        df_kr = fdr.StockListing('KOSPI')
        kr_mc_80 = df_kr.sort_values('Marcap', ascending=False).head(80)
        kr_vol_20 = df_kr[~df_kr['Code'].isin(kr_mc_80['Code'])].sort_values('Volume', ascending=False).head(20)
        kr_combined = pd.concat([kr_mc_80, kr_vol_20])
        for _, row in kr_combined.iterrows():
            ticker_dict[f"{row['Code']}.KS"] = row['Name']
    elif market == "us":
        df_us = fdr.StockListing('S&P500')
        for _, row in df_us.head(100).iterrows():
            ticker_dict[row['Symbol']] = row['Name']
    elif market == "jp":
        jp_codes = {"7203": "도요타자동차", "6758": "소니그룹", "8306": "미쓰비시UFJ", "8035": "도쿄일렉트론", "9984": "소프트뱅크"}
        for code, name in jp_codes.items():
            ticker_dict[f"{code}.T"] = name
    return ticker_dict

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
    
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['MA60'] = df['Close'].rolling(window=60).mean()
    df['High20'] = df['High'].rolling(window=20).max()
    df['Vol20'] = df['Volume'].rolling(window=20).mean()
    
    return df

def get_deep_analysis(ticker, hist_df):
    stock = yf.Ticker(ticker)
    info = stock.info
    prev_close = hist_df['Close'].iloc[-2]
    current = round(hist_df['Close'].iloc[-1], 2)
    change_pct = round(((current - prev_close) / prev_close) * 100, 2)
    per = round(info.get('trailingPE', 0), 2) if info.get('trailingPE') else 'N/A'
    pbr = round(info.get('priceToBook', 0), 2) if info.get('priceToBook') else 'N/A'
    roe = f"{round(info.get('returnOnEquity', 0) * 100, 2)}%" if info.get('returnOnEquity') else 'N/A'
    return current, change_pct, per, pbr, roe

# ==========================================
# 매도 시그널 모니터링 (유지)
# ==========================================
def check_sell_signals(sheet, market_name, mode, webhook_url):
    print(f"🔍 [{market_name}] 시장 [{mode.upper()}] 보유 종목 매도 시그널 검사 중...")
    try:
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            row_num = idx + 2
            strategy_col = '전략(DCA/단타)' if '전략(DCA/단타)' in row else '전략 (DCA/단타)'
            target_strategy = 'DCA' if mode == 'dca' else ('불장' if mode == 'bull' else '단타')
            
            if row.get('시장') != market_name or row.get(strategy_col, '') != target_strategy:
                continue
                
            status = str(row.get('상태', '')).strip()
            if status in ['매도알림완료', '매도완료']:
                continue
                
            ticker = row.get('티커')
            if not ticker: continue
            entry_price = float(row.get('매수가', 0))
            if entry_price == 0: continue
            stock_name = row.get('종목명', ticker)
            
            stock = yf.Ticker(ticker)
            hist = stock.history(period="15d", interval="1h") if mode in ["danta", "bull"] else stock.history(period="60d", interval="1d")
            if len(hist) < 26: continue
            
            df = calculate_indicators(hist)
            current_price = df['Close'].iloc[-1]
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            rsi = round(df['RSI'].iloc[-1], 2)
            macd_curr = df['MACD_Hist'].iloc[-1]
            macd_prev = df['MACD_Hist'].iloc[-2]
            
            sell_reason = ""
            if profit_pct >= 5.0:
                sell_reason = f"🎯 목표 수익률 5% 달성! (현재 **+{profit_pct:.2f}%**)"
            elif mode == "danta" and (rsi > 70 or (macd_prev > 0 and macd_curr < 0)):
                sell_reason = f"🚨 단기 상승 추세가 꺾였습니다! (RSI: {rsi}, MACD 데드크로스)"
            elif mode == "bull" and (macd_prev > 0 and macd_curr < 0):
                sell_reason = f"🚨 야수의 심장 모멘텀 이탈! (MACD 데드크로스) 빠른 청산 권장!"
            elif mode == "dca" and rsi > 70:
                sell_reason = f"🚨 과열 구간 진입! 일부 수익 실현을 고려해보세요. (RSI: {rsi})"
                
            if sell_reason:
                currency = "$" if "us" in market_name.lower() or market_name == "미국" else "원"
                msg_title = f"🔔 [익절/매도 타이밍 포착!] {stock_name} ({ticker})"
                msg_desc = (f"펀드매니저 봇의 매도 권유 알림입니다.\n**{sell_reason}**\n\n"
                            f"진입가: {entry_price:,.2f}{currency} ➡️ 현재가: **{current_price:,.2f}**{currency}")
                fields = [
                    {"name": "📈 수익률", "value": f"**{profit_pct:.2f}%**", "inline": True},
                    {"name": "⏱️ 현재 RSI", "value": f"`{rsi}`", "inline": True}
                ]
                send_discord_msg(webhook_url, msg_title, msg_desc, 3066993, fields)
                sheet.update_cell(row_num, 8, '매도알림완료')
                time.sleep(1)
                
    except Exception as e:
        print(f"❌ 매도 시그널 검사 실패: {e}")

# ==========================================
# 핵심 엔진 (종합 알림 기능 탑재!)
# ==========================================
def run_bot(market, mode):
    ticker_dict = get_universe_dict(market)
    tickers = list(ticker_dict.keys())
    market_name = {"kr": "국내", "us": "미국", "jp": "일본"}[market]
    dca_webhook = WEBHOOK_URLS[f"{market.upper()}_DCA"]
    danta_webhook = WEBHOOK_URLS[f"{market.upper()}_DANTA"]
    webhook_to_use = dca_webhook if mode == 'dca' else danta_webhook
    
    sheet = init_gsheets()
    holding_tickers = []
    
    if sheet:
        check_sell_signals(sheet, market_name, mode, webhook_to_use)
        records = sheet.get_all_records()
        holding_tickers = [r.get('티커') for r in records if r.get('상태') in ['보유중', '매도알림완료']]
    
    print(f"\n⚙️ [{market_name}] 시장 [{mode.upper()}] 신규 타점 스캔 시작...")
    
    # 💡 [핵심] 이번 스캔에서 포착된 종목들을 모아둘 장바구니 리스트!
    detected_stocks = []
    
    for i, ticker in enumerate(tickers):
        try:
            if ticker in holding_tickers:
                continue

            if (i+1) % 20 == 0: print(f"   진행률: {i+1}/{len(tickers)}")
            stock = yf.Ticker(ticker)
            stock_name = ticker_dict[ticker]
            
            if mode == "dca":
                hist = stock.history(period="60d", interval="1d")
                if len(hist) >= 26:
                    df = calculate_indicators(hist)
                    rsi = round(df['RSI'].iloc[-1], 2)
                    bb_lower = round(df['BB_Lower'].iloc[-1], 2)
                    current_price = df['Close'].iloc[-1]
                    
                    if rsi < 30 and current_price <= bb_lower:
                        curr, chg, per, pbr, roe = get_deep_analysis(ticker, df)
                        if sheet:
                            time_str = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d')
                            sheet.append_row([time_str, "DCA", market_name, stock_name, ticker, curr, 500000, "보유중"])
                        
                        # 장바구니에 담기
                        detected_stocks.append({
                            "name": stock_name, "ticker": ticker, "curr": curr, "chg": chg, 
                            "reason": f"RSI {rsi} / 볼린저 밴드 하단 이탈", "per": per, "roe": roe
                        })

            elif mode == "danta":
                hist = stock.history(period="15d", interval="1h")
                if len(hist) >= 26:
                    df = calculate_indicators(hist)
                    rsi_h = round(df['RSI'].iloc[-1], 2)
                    macd_curr = df['MACD_Hist'].iloc[-1]
                    macd_prev = df['MACD_Hist'].iloc[-2]
                    
                    if rsi_h < 40 and (macd_prev < 0 and macd_curr > 0):
                        curr, chg, per, pbr, roe = get_deep_analysis(ticker, df)
                        if sheet:
                            time_str = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')
                            sheet.append_row([time_str, "단타", market_name, stock_name, ticker, curr, 500000, "보유중"])

                        detected_stocks.append({
                            "name": stock_name, "ticker": ticker, "curr": curr, "chg": chg, 
                            "reason": f"RSI {rsi_h} / MACD 골든크로스", "per": per, "roe": roe
                        })

            elif mode == "bull":
                hist = stock.history(period="90d", interval="1d")
                if len(hist) >= 65:
                    df = calculate_indicators(hist)
                    
                    curr_close = df['Close'].iloc[-1]
                    curr_vol = df['Volume'].iloc[-1]
                    ma20 = df['MA20'].iloc[-1]
                    ma60 = df['MA60'].iloc[-1]
                    high20_prev = df['High20'].iloc[-2]
                    vol20_prev = df['Vol20'].iloc[-2]
                    rsi = round(df['RSI'].iloc[-1], 2)
                    
                    bull_reason = ""
                    if curr_close > ma60 and (ma20 * 0.98 <= curr_close <= ma20 * 1.02) and rsi < 55:
                        bull_reason = "📉 [눌림목] 20일선 부근 예쁜 조정"
                    elif curr_close > high20_prev and curr_vol > (vol20_prev * 2):
                        bull_reason = "🚀 [돌파] 거래량 폭발 전고점 돌파"
                    
                    if bull_reason:
                        curr, chg, per, pbr, roe = get_deep_analysis(ticker, df)
                        if sheet:
                            time_str = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')
                            sheet.append_row([time_str, "불장", market_name, stock_name, ticker, curr, 500000, "보유중"])

                        detected_stocks.append({
                            "name": stock_name, "ticker": ticker, "curr": curr, "chg": chg, 
                            "reason": bull_reason, "per": per, "roe": roe
                        })
            time.sleep(0.1) 
        except Exception as e:
            pass

    # ==========================================
    # 💡 [핵심] 스캔 종료 후 종합 리포트 한 번에 쏘기!
    # ==========================================
    if detected_stocks:
        icon = "🦁" if mode == "bull" else ("⚡" if mode == "danta" else "🛍️")
        strategy_kr = "야수의 심장(불장)" if mode == "bull" else ("단기 반등(단타)" if mode == "danta" else "바겐세일(DCA)")
        
        msg_title = f"{icon} [{market_name}] {strategy_kr} 타점 종합 리포트 (총 {len(detected_stocks)}종목)"
        
        if mode == "bull":
            msg_desc = "도파민 펀드매니저 출동! 🔥 상승장에 올라탈 종목들을 종합해 왔습니다. 꽉 잡으세요!"
            color = 16753920 # 주황색
        elif mode == "danta":
            msg_desc = "단타 요정 출동! 🧚‍♂️ 방금 고개를 든 단기 반등 예상 종목 리스트입니다."
            color = 16711680 # 빨간색
        else:
            msg_desc = "공포에 사서 환희에 팔 시간입니다! 📉 지하실에 내려간 종목 리스트입니다."
            color = 255 # 파란색

        fields = []
        # 디스코드 제한(25개)을 막기 위해 최대 15개까지만 리포트에 담습니다.
        for item in detected_stocks[:15]:
            price_str = f"${item['curr']:,.2f}" if market == "us" else f"{item['curr']:,.0f}원"
            fields.append({
                "name": f"🔹 {item['name']} ({item['ticker']})",
                "value": f"**현재가:** {price_str} ({item['chg']}%)\n**사유:** {item['reason']}\n**가치/수익:** PER {item['per']} / ROE {item['roe']}",
                "inline": False
            })

        if len(detected_stocks) > 15:
            overflow = len(detected_stocks) - 15
            fields.append({
                "name": f"➕ 그 외 {overflow}건의 포착 종목 생략",
                "value": "너무 많은 종목이 포착되어 리포트에 담지 못했습니다. 가상 펀드(구글 시트)를 확인하세요!",
                "inline": False
            })

        send_discord_msg(webhook_to_use, msg_title, msg_desc, color, fields)
        print(f"✨ 종합 알림 전송 완료! (포착 수: {len(detected_stocks)}건)")
    else:
        print(f"💤 이번 스캔에서는 조건에 맞는 종목이 발견되지 않았습니다.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    parser.add_argument('--mode', type=str, required=True, choices=['dca', 'danta', 'bull']) 
    args = parser.parse_args()
    
    run_bot(args.market, args.mode)
