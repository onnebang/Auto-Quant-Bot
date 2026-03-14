import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
import os
import json
import gspread
from google.oauth2.service_account import Credentials
import time
import argparse # 💡 에러의 원인이었던 녀석 추가!

# 한국 시간(KST) 구하는 함수
def get_kst_time():
    return datetime.utcnow() + timedelta(hours=9)

WEBHOOK_URLS = {
    "KR_DANTA_REPORT": os.getenv("WEBHOOK_KR_DANTA"),
    "US_DANTA_REPORT": os.getenv("WEBHOOK_US_DANTA") 
}

def send_discord_report(webhook_url, title, description, color, fields=[]):
    if not webhook_url or not webhook_url.startswith("http"): return
    data = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"🏆 펀드매니저 봇 | 결산 시간: {get_kst_time().strftime('%Y-%m-%d %H:%M')}"},
            "thumbnail": {"url": "https://cdn-icons-png.flaticon.com/512/3258/3258503.png"}
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

def generate_daily_report(market):
    print(f"\n⚙️ [{market.upper()}] 시장 당일 단타 성적표 작성 중...")
    webhook_key = f"{market.upper()}_DANTA_REPORT"
    webhook_url = WEBHOOK_URLS[webhook_key]
    sheet = init_gsheets()
    if not sheet: return

    try:
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        if df.empty: return

        # 💡 오늘 날짜도 무조건 한국 시간 기준으로 판단!
        today_str = get_kst_time().strftime('%Y-%m-%d')
        print(f"   오늘 날짜(KST): {today_str}")

        df['날짜'] = pd.to_datetime(df['날짜'])
        today_trades = df[ (df['날짜'].dt.strftime('%Y-%m-%d') == today_str) & (df['전략(DCA/단타)'] == '단타') ]
        
        if today_trades.empty:
            msg_title = f"☕ [오늘의 {market.upper()} 단타 성적표] - 진입 없음"
            msg_desc = "오늘은 봇이 조용했습니다. 매니저도 커피 한 잔의 여유를 즐기겠습니다."
            send_discord_report(webhook_url, msg_title, msg_desc, 255)
            return

        trades_summary = []
        total_pnl = 0
        total_investment = 0
        
        for _, row in today_trades.iterrows():
            ticker = row['티커']
            stock_name = row['종목명']
            entry_price = float(row['매수가'])
            investment = float(row['매수금액(원)']) if '매수금액(원)' in row else 500000
            total_investment += investment
            
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d", interval="5m")
            if hist.empty: continue
                
            day_high = round(hist['High'].max(), 2)
            day_close = round(hist['Close'].iloc[-1], 2)
            
            high_return = round(((day_high - entry_price) / entry_price) * 100, 2)
            current_return = round(((day_close - entry_price) / entry_price) * 100, 2)
            pnl = round(investment * (current_return / 100), 0)
            total_pnl += pnl
            
            trade_field = {
                "name": f"🏆 {stock_name} ({ticker})",
                "value": (f"**진입:** {entry_price:,.0f}원 / **현재:** {day_close:,.0f}원\n"
                          f"**당일 최고 수익률:** `{high_return}`%\n"
                          f"**현재 수익률:** **`{current_return}`**% (PNL: `{pnl:,.0f}`원)"),
                "inline": False
            }
            trades_summary.append(trade_field)
            time.sleep(0.5)
            
        initial_balance = 10000000
        total_return = round(((total_pnl / initial_balance) * 100), 2)
        final_balance = initial_balance + total_pnl
        
        msg_title = f"🏆 [오늘의 {market.upper()} 단타 성적표] - 펀드매니저 브리핑"
        msg_desc = (f"여러분! 오늘 단타봇이 1천만 원 시드로 굴린 성적표가 나왔습니다.\n"
                    f"총 {len(today_trades)}종목에 투자했습니다. (총 투자금: {total_investment:,.0f}원)")
        
        summary_fields = [
            {"name": "💵 오늘의 총수익금", "value": f"**`{total_pnl:,.0f}`**원 ({total_return}%)", "inline": True},
            {"name": "💼 현재 가상 잔고", "value": f"{final_balance:,.0f}원", "inline": True}
        ]
        
        final_fields = trades_summary + summary_fields
        color = 16711680 if total_pnl > 0 else 255
        
        send_discord_report(webhook_url, msg_title, msg_desc, color, final_fields)
        print(f"✨ [{market.upper()}] 결산 리포트 전송 완료!")
        
    except Exception as e:
        print(f"❌ [{market.upper()}] 결산 실패: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    args = parser.parse_args()
    generate_daily_report(args.market)
