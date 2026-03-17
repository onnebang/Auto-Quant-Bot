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
# 🌟 [신규 기능] 매도 시그널 모니터링
# ==========================================
def check_sell_signals(sheet, market_name, mode, webhook_url):
    print(f"🔍 [{market_name}] 시장 [{mode.upper()}] 보유 종목 매도 시그널 검사 중...")
    try:
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            row_num = idx + 2 # 데이터는 2행부터 시작
            strategy_col = '전략(DCA/단타)' if '전략(DCA/단타)' in row else '전략 (DCA/단타)'
            
            # 현재 스캔 중인 시장/모드와 일치하는 종목만 검사
            if row.get('시장') != market_name or row.get(strategy_col, '') != ('DCA' if mode=='dca' else '단타'):
                continue
                
            # 이미 매도 알림을 보낸 종목은 패스
            status = str(row.get('상태', '')).strip()
            if status in ['매도알림완료', '매도완료']:
                continue
                
            ticker = row.get('티커')
            if not ticker: continue
            entry_price = float(row.get('매수가', 0))
            if entry_price == 0: continue
            stock_name = row.get('종목명', ticker)
            
            # 데이터 수집 및 지표 연산
            stock = yf.Ticker(ticker)
            hist = stock.history(period="15d", interval="1h") if mode == "danta" else stock.history(period="60d", interval="1d")
            if len(hist) < 26: continue
            
            df = calculate_indicators(hist)
            current_price = df['Close'].iloc[-1]
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            
            rsi = round(df['RSI'].iloc[-1], 2)
            macd_curr = df['MACD_Hist'].iloc[-1]
            macd_prev = df['MACD_Hist'].iloc[-2]
            
            sell_reason = ""
            # 🎯 매도 조건 1: 목표 수익률 5% 달성 (익절 권유)
            if profit_pct >= 5.0:
                sell_reason = f"🎯 목표 수익률 5% 달성! (현재 **+{profit_pct:.2f}%**)"
            # 🚨 매도 조건 2: 지표 하락 반전 (MACD 데드크로스 또는 RSI 70 이상 과열)
            elif mode == "danta" and (rsi > 70 or (macd_prev > 0 and macd_curr < 0)):
                sell_reason = f"🚨 단기 상승 추세가 꺾였습니다! (RSI: {rsi}, MACD 데드크로스)"
            elif mode == "dca" and rsi > 70:
                sell_reason = f"🚨 과열 구간 진입! 일부 수익 실현을 고려해보세요. (RSI: {rsi})"
                
            # 매도 조건에 해당하면 디스코드 알림 발송!
            if sell_reason:
                currency = "$" if "us" in market_name.lower() or market_name == "미국" else "원"
                msg_title = f"🔔 [익절/매도 타이밍 포착!] {stock_name} ({ticker})"
                msg_desc = (f"펀드매니저 봇의 매도 권유 알림입니다.\n**{sell_reason}**\n\n"
                            f"진입가: {entry_price:,.2f}{currency} ➡️ 현재가: **{current_price:,.2f}**{currency}")
                fields = [
                    {"name": "📈 수익률", "value": f"**{profit_pct:.2f}%**", "inline": True},
                    {"name": "⏱️ 현재 RSI", "value": f"`{rsi}`", "inline": True}
                ]
                
                # 매도 알림은 눈에 띄게 에메랄드(초록) 색상으로! (color: 3066993)
                send_discord_msg(webhook_url, msg_title, msg_desc, 3066993, fields)
                
                # 시트의 8번째 열(H열)에 '매도알림완료' 기록하여 스팸 방지
                sheet.update_cell(row_num, 8, '매도알림완료')
                time.sleep(1)
                
    except Exception as e:
        print(f"❌ 매도 시그널 검사 실패: {e}")

# ==========================================
# 핵심 엔진
# ==========================================
def run_bot(market, mode):
    ticker_dict = get_universe_dict(market)
    tickers = list(ticker_dict.keys())
    market_name = {"kr": "국내", "us": "미국", "jp": "일본"}[market]
    dca_webhook = WEBHOOK_URLS[f"{market.upper()}_DCA"]
    danta_webhook = WEBHOOK_URLS[f"{market.upper()}_DANTA"]
    webhook_to_use = danta_webhook if mode == 'danta' else dca_webhook
    
    sheet = init_gsheets()
    
    # ⭐ 신규 진입 스캔 전, 우리가 가진 종목들 중 팔아야 할 게 있는지 먼저 검사!
    if sheet:
        check_sell_signals(sheet, market_name, mode, webhook_to_use)
    
    print(f"\n⚙️ [{market_name}] 시장 [{mode.upper()}] 신규 타점 스캔 시작...")
    
    for i, ticker in enumerate(tickers):
        try:
            if (i+1) % 20 == 0: print(f"   진행률: {i+1}/{len(tickers)}")
            stock = yf.Ticker(ticker)
            stock_name = ticker_dict[ticker]
            
            # [전략 A] DCA
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
                            # 💡 H열(8번째 칸)에 "보유중" 이라는 상태값 추가
                            sheet.append_row([time_str, "DCA", market_name, stock_name, ticker, curr, 500000, "보유중"])
                        
                        fields = [
                            {"name": "📈 현재가", "value": f"**${curr}** ({chg}%)" if market=="us" else f"**{curr}원** ({chg}%)", "inline": True},
                            {"name": "🌡️ RSI", "value": f"`{rsi}`", "inline": True},
                            {"name": "📊 가치/수익", "value": f"PER {per} / ROE {roe}", "inline": True}
                        ]
                        
                        msg_title = f"🛍️ [바겐세일 줍줍 타이밍!] {stock_name} ({ticker})"
                        msg_desc = (f"여러분! {stock_name} 주가가 볼린저 밴드를 뚫고 지하실로 내려갔습니다.\n"
                                    f"공포에 사서 환희에 팔 시간입니다! 펀드매니저 봇이 가상 계좌에서 50만 원어치 자동 매수했습니다. 💸")
                        
                        send_discord_msg(dca_webhook, msg_title, msg_desc, 16711680, fields)

            # [전략 B] 단타 / 스윙
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
                            # 💡 H열(8번째 칸)에 "보유중" 이라는 상태값 추가
                            sheet.append_row([time_str, "단타", market_name, stock_name, ticker, curr, 500000, "보유중"])

                        fields = [
                            {"name": "📈 현재가", "value": f"**${curr}** ({chg}%)" if market=="us" else f"**{curr}원** ({chg}%)", "inline": True},
                            {"name": "⏱️ 60분봉 RSI", "value": f"`{rsi_h}`", "inline": True},
                            {"name": "📊 가치/수익", "value": f"PER {per} / ROE {roe}", "inline": True}
                        ]
                        
                        msg_title = f"⚡ [단기 반등 타이밍!] {stock_name} ({ticker})"
                        msg_desc = (f"단타 요정 출동! 🧚‍♂️ 하락하던 {stock_name} 주가가 방금 고개를 들고 MACD 골든크로스를 만들었습니다.\n"
                                    f"짧게 치고 빠지실 분들, 타이밍 한 번 노려보시죠! (당일 마감 후 성적표 제출하겠습니다 📝)")
                        
                        color = 16711680 if chg > 0 else 255
                        send_discord_msg(danta_webhook, msg_title, msg_desc, color, fields)
                        
            time.sleep(0.1)
        except Exception as e:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    parser.add_argument('--mode', type=str, required=True, choices=['dca', 'danta'])
    args = parser.parse_args()
    
    run_bot(args.market, args.mode)
