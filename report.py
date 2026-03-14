import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
import os
import json
import gspread
from google.oauth2.service_account import Credentials
import time

# ==========================================
# 1. 환경 변수 및 디스코드 웹훅 설정
# ==========================================
# 결산 리포트 전송을 위한 웹훅 (아까 4개 방 웹훅 주소 중 하나를 쓰거나, 전용 방을 만드세요.)
WEBHOOK_URLS = {
    "KR_DANTA_REPORT": os.getenv("WEBHOOK_KR_DANTA"), # 국내 단타 방
    "US_DANTA_REPORT": os.getenv("WEBHOOK_US_DANTA")  # 미국 단타 방
}

def send_discord_report(webhook_url, title, description, color, fields=[]):
    if not webhook_url or not webhook_url.startswith("http"): return
    data = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"🏆 펀드매니저 봇 | 결산 시간: {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
            "thumbnail": {"url": "https://cdn-icons-png.flaticon.com/512/3258/3258503.png"} # 트로피 아이콘
        }]
    }
    requests.post(webhook_url, json=data)

# ==========================================
# 2. 구글 시트 연동 함수 (main.py와 동일)
# ==========================================
def init_gsheets():
    try:
        json_creds = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
        if not json_creds: 
            print("⚠️ 구글 시트 JSON 키가 없습니다. 결산을 중단합니다.")
            return None
        creds_dict = json.loads(json_creds)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        sheet = client.open("QuantBot_Fund").worksheet("Portfolio")
        return sheet
    except Exception as e:
        print(f"❌ 구글 시트 연동 실패: {e}")
        return None

# ==========================================
# 3. 수익률 계산 및 리포트 생성 엔진
# ==========================================
def generate_daily_report(market):
    print(f"\n⚙️ [{market.upper()}] 시장 당일 단타 성적표 작성 중...")
    
    webhook_key = f"{market.upper()}_DANTA_REPORT"
    webhook_url = WEBHOOK_URLS[webhook_key]
    sheet = init_gsheets()
    
    if not sheet: return

    try:
        # 시트 데이터 전체 로드
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        
        if df.empty:
            print("⚠️ 시트에 데이터가 없습니다.")
            return

        # 오늘 날짜 (YYYY-MM-DD 형식)
        # 중요: 구글 시트에 적힌 날짜 형식과 일치해야 합니다.
        today_str = datetime.now().strftime('%Y-%m-%d')
        print(f"   오늘 날짜: {today_str}")

        # 오늘 날짜에 진입한 '단타' 종목만 필터링
        df['날짜'] = pd.to_datetime(df['날짜']) # 날짜 형식으로 변환
        today_trades = df[ (df['날짜'].dt.strftime('%Y-%m-%d') == today_str) & (df['전략 (DCA/단타)'] == '단타') ]
        
        if today_trades.empty:
            print(f"   오늘 {market.upper()} 시장에서는 단타 진입이 없었습니다.")
            msg_title = f"☕ [오늘의 {market.upper()} 단타 성적표] - 진입 없음"
            msg_desc = "오늘은 봇이 조용했습니다. 매니저도 커피 한 잔의 여유를 즐기겠습니다."
            send_discord_report(webhook_url, msg_title, msg_desc, 255) # 파란색
            return

        print(f"   오늘 포착된 단타 종목 {len(today_trades)}개...")

        # 수익률 계산을 위한 리스트
        trades_summary = []
        total_pnl = 0
        total_investment = 0
        
        for _, row in today_trades.iterrows():
            ticker = row['티커']
            stock_name = row['종목명']
            entry_price = float(row['매수가'])
            investment = float(row['매수금액 (원)'])
            total_investment += investment
            
            stock = yf.Ticker(ticker)
            # 오늘 하루의 데이터 (분봉/시간봉) 가져오기
            hist = stock.history(period="1d", interval="5m")
            
            if hist.empty:
                print(f"   {ticker} 데이터를 가져오지 못했습니다.")
                continue
                
            day_high = round(hist['High'].max(), 2) # 당일 최고가
            day_close = round(hist['Close'].iloc[-1], 2) # 현재가(종가)
            
            # 수익률 계산
            high_return = round(((day_high - entry_price) / entry_price) * 100, 2)
            current_return = round(((day_close - entry_price) / entry_price) * 100, 2)
            
            # 실현 수익금 계산 (원금 * 현재 수익률)
            pnl = round(investment * (current_return / 100), 0)
            total_pnl += pnl
            
            # 디스코드 필드 구성
            trade_field = {
                "name": f"🏆 {stock_name} ({ticker})",
                "value": (f"**진입:** {entry_price:,.0f}원 / **현재:** {day_close:,.0f}원\n"
                          f"**당일 최고 수익률:** `{high_return}`%\n"
                          f"**현재 수익률:** **`{current_return}`**% (PNL: `{pnl:,.0f}`원)"),
                "inline": False
            }
            trades_summary.append(trade_field)
            time.sleep(0.5) # 야후 서버 부하 방지
            
        # 총 수익률 및 잔고 계산
        # ⚠️ 중요: 가상 펀드의 초기 원금은 1,000만 원으로 가정합니다.
        # 이 부분은 추후 시트의 예수금 칸과 연동하면 더 완벽해집니다.
        initial_balance = 10000000
        total_return = round(((total_pnl / initial_balance) * 100), 2)
        final_balance = initial_balance + total_pnl
        
        # 성적표 임베드 구성
        msg_title = f"🏆 [오늘의 {market.upper()} 단타 성적표] - 펀드매니저 브리핑"
        msg_desc = (f"여러분! 오늘 단타봇이 1천만 원 시드로 굴린 성적표가 나왔습니다.\n"
                    f"총 {len(today_trades)}종목에 투자했습니다. (총 투자금: {total_investment:,.0f}원)")
        
        # 총평 필드 추가
        summary_fields = [
            {"name": "💵 오늘의 총수익금", "value": f"**`{total_pnl:,.0f}`**원 ({total_return}%)", "inline": True},
            {"name": "💼 현재 가상 잔고", "value": f"{final_balance:,.0f}원", "inline": True}
        ]
        
        final_fields = trades_summary + summary_fields
        color = 16711680 if total_pnl > 0 else 255 # 수익나면 빨강, 손실나면 파랑
        
        send_discord_report(webhook_url, msg_title, msg_desc, color, final_fields)
        print(f"✨ [{market.upper()}] 결산 리포트 전송 완료!")
        
    except Exception as e:
        print(f"❌ [{market.upper()}] 결산 실패: {e}")

# ==========================================
# 4. 실행부
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    args = parser.parse_args()
    
    # 해당 시장의 결산 리포트 실행
    generate_daily_report(args.market)
