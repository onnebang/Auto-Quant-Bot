import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
import os
import json
import gspread
from google.oauth2.service_account import Credentials
import time
import argparse

# 한국 시간(KST) 구하는 함수
def get_kst_time():
    return datetime.utcnow() + timedelta(hours=9)

# 웹훅 URL
WEBHOOK_URLS = {
    "KR_DANTA_REPORT": os.getenv("WEBHOOK_KR_DANTA"),
    "US_DANTA_REPORT": os.getenv("WEBHOOK_US_DANTA"),
    "KR_DCA_REPORT": os.getenv("WEBHOOK_KR_DCA"),
    "US_DCA_REPORT": os.getenv("WEBHOOK_US_DCA")
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

# ==========================================
# [기능 1] 당일 단타 & 불장 결산 (통합)
# ==========================================
def generate_daily_report(market, mode):
    # 💡 모드에 따라 이름과 아이콘을 다르게 설정합니다.
    strategy_name = "단타" if mode == "danta" else "불장"
    icon = "🏆" if mode == "danta" else "🦁"
    
    print(f"\n⚙️ [{market.upper()}] 시장 당일 {strategy_name} 성적표 작성 중...")
    
    # 불장 모드도 단타 채널에 성적표를 쏩니다.
    webhook_url = WEBHOOK_URLS[f"{market.upper()}_DANTA_REPORT"]
    sheet = init_gsheets()
    if not sheet: return

    try:
        df = pd.DataFrame(sheet.get_all_records())
        if df.empty: return

        today_str = get_kst_time().strftime('%Y-%m-%d')
        df['날짜'] = pd.to_datetime(df['날짜'])
        strategy_col = '전략(DCA/단타)' if '전략(DCA/단타)' in df.columns else '전략 (DCA/단타)'
        
        market_korean = "국내" if market == "kr" else "미국" if market == "us" else "일본"
        
        today_trades = df[ (df['날짜'].dt.strftime('%Y-%m-%d') == today_str) & 
                           (df[strategy_col] == strategy_name) & 
                           (df['시장'] == market_korean) ]
        
        if today_trades.empty:
            msg_title = f"☕ [오늘의 {market.upper()} {strategy_name} 성적표] - 진입 없음"
            msg_desc = "오늘은 봇이 조용했습니다. 매니저도 커피 한 잔의 여유를 즐기겠습니다."
            send_discord_report(webhook_url, msg_title, msg_desc, 255)
            return

        trades_summary, total_pnl, total_investment = [], 0, 0
        
        for _, row in today_trades.iterrows():
            ticker, stock_name, entry_price = row['티커'], row['종목명'], float(row['매수가'])
            investment = float(row.get('매수금액(원)', row.get('매수금액 (원)', 500000)))
            total_investment += investment
            
            hist = yf.Ticker(ticker).history(period="1d", interval="5m")
            if hist.empty: continue
                
            day_high = round(hist['High'].max(), 2)
            day_close = round(hist['Close'].iloc[-1], 2)
            
            high_return = round(((day_high - entry_price) / entry_price) * 100, 2)
            current_return = round(((day_close - entry_price) / entry_price) * 100, 2)
            pnl = round(investment * (current_return / 100), 0)
            total_pnl += pnl
            
            price_format = f"${entry_price:,.2f} / **${day_close:,.2f}**" if market == "us" else f"{entry_price:,.0f}원 / **{day_close:,.0f}원**"
            
            trades_summary.append({
                "name": f"{icon} {stock_name} ({ticker})",
                "value": f"**진입/현재:** {price_format}\n**당일 최고 수익률:** `{high_return}`%\n**현재 수익률:** **`{current_return}`**% (PNL: `{pnl:,.0f}`원)",
                "inline": False
            })
            time.sleep(0.5)

        # 💡 [신규 추가] 디스코드 25개 제한 방어 로직!
        if len(trades_summary) > 20:
            overflow_count = len(trades_summary) - 20
            trades_summary = trades_summary[:20]
            trades_summary.append({
                "name": f"➕ 그 외 {overflow_count}건의 거래 생략",
                "value": "너무 많은 종목이 포착되어 리포트에 모두 담지 못했습니다. (구글 시트를 확인하세요!)",
                "inline": False
            })
            
        initial_balance = 10000000
        
        total_return = round(((total_pnl / initial_balance) * 100), 2)
        
        msg_title = f"{icon} [오늘의 {market.upper()} {strategy_name} 성적표]"
        msg_desc = f"오늘 {strategy_name} 봇이 총 {len(today_trades)}종목에 탑승했습니다. (총 투자금: {total_investment:,.0f}원)"
        summary_fields = trades_summary + [
            {"name": "💵 오늘의 총수익금", "value": f"**`{total_pnl:,.0f}`**원 ({total_return}%)", "inline": True},
            {"name": "💼 현재 가상 잔고", "value": f"{initial_balance + total_pnl:,.0f}원", "inline": True}
        ]
        
        send_discord_report(webhook_url, msg_title, msg_desc, 16711680 if total_pnl > 0 else 255, summary_fields)
        print(f"✨ [{market.upper()}] {strategy_name} 결산 완료!")
    except Exception as e:
        print(f"❌ {strategy_name} 결산 실패: {e}")

# ==========================================
# [기능 2] 주간 DCA 펀드 운용 리포트 (유지)
# ==========================================
def generate_weekly_dca_report(market):
    print(f"\n⚙️ [{market.upper()}] 시장 주간 DCA 포트폴리오 결산 중...")
    webhook_url = WEBHOOK_URLS[f"{market.upper()}_DCA_REPORT"]
    sheet = init_gsheets()
    if not sheet: return

    try:
        df = pd.DataFrame(sheet.get_all_records())
        if df.empty: return

        strategy_col = '전략(DCA/단타)' if '전략(DCA/단타)' in df.columns else '전략 (DCA/단타)'
        market_korean = "국내" if market == "kr" else "미국" if market == "us" else "일본"
        
        dca_trades = df[ (df[strategy_col] == 'DCA') & (df['시장'] == market_korean) ].copy()
            
        if dca_trades.empty:
            print(f"   {market.upper()} 시장 DCA 누적 기록이 없습니다.")
            return

        total_investment, total_current_value = 0, 0
        performance_dict, pnl_dict = {}, {} 

        for _, row in dca_trades.iterrows():
            ticker, stock_name, entry_price = row['티커'], row['종목명'], float(row['매수가'])
            investment = float(row.get('매수금액(원)', row.get('매수금액 (원)', 500000)))
            shares = investment / entry_price
            
            hist = yf.Ticker(ticker).history(period="1d")
            if hist.empty: continue
            
            current_price = float(hist['Close'].iloc[-1])
            current_value = shares * current_price
            
            total_investment += investment
            total_current_value += current_value
            
            return_pct = ((current_price - entry_price) / entry_price) * 100
            pnl_amount = current_value - investment 
            
            performance_dict[stock_name] = return_pct
            pnl_dict[stock_name] = pnl_amount 
            time.sleep(0.5)

        total_pnl = total_current_value - total_investment
        total_return_pct = (total_pnl / total_investment) * 100 if total_investment > 0 else 0
        
        best_stock = max(performance_dict, key=performance_dict.get)
        worst_stock = min(performance_dict, key=performance_dict.get)
        
        sorted_perf = sorted(performance_dict.items(), key=lambda item: item[1], reverse=True)
        portfolio_str = ""
        for name, ret in sorted_perf:
            pnl = pnl_dict[name]
            icon = "🔴" if ret > 0 else ("🔵" if ret < 0 else "⚪")
            sign = "+" if pnl > 0 else "" 
            portfolio_str += f"{icon} **{name}**: `{ret:.2f}%` ({sign}{pnl:,.0f}원)\n"

        if len(portfolio_str) > 1000:
            portfolio_str = portfolio_str[:990] + "...\n(이하 생략)"
        
        msg_title = f"💼 [{market.upper()}] 퀀트 봇 가상 펀드 주간 리포트"
        msg_desc = (f"여러분! 한 주 동안 봇이 바닥에서 줍줍한 DCA 가상 펀드 운용 결과입니다.\n"
                    f"꾸준히 모아가는 장기 투자의 힘을 확인해보세요! 🚀")
        
        fields = [
            {"name": "💰 총 투자 원금", "value": f"{total_investment:,.0f}원", "inline": True},
            {"name": "📈 현재 평가금액", "value": f"**{total_current_value:,.0f}원**", "inline": True},
            {"name": "📊 펀드 총수익률", "value": f"**`{total_return_pct:,.2f}%`** (PNL: {total_pnl:,.0f}원)", "inline": False},
            {"name": f"🥇 최고 효자 종목", "value": f"**{best_stock}** (`{performance_dict[best_stock]:.2f}%`)", "inline": True},
            {"name": f"🩹 아픈 손가락", "value": f"**{worst_stock}** (`{performance_dict[worst_stock]:.2f}%`)", "inline": True},
            {"name": "📋 전체 포트폴리오 현황", "value": portfolio_str, "inline": False}
        ]
        
        color = 16711680 if total_pnl > 0 else 255
        send_discord_report(webhook_url, msg_title, msg_desc, color, fields)
        print(f"✨ [{market.upper()}] DCA 주간 리포트 전송 완료!")

    except Exception as e:
        print(f"❌ DCA 결산 실패: {e}")

# ==========================================
# 실행부
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True, choices=['kr', 'us', 'jp'])
    # 💡 choices에 'bull' 추가!
    parser.add_argument('--mode', type=str, required=True, choices=['danta', 'dca', 'bull'])
    args = parser.parse_args()
    
    # 단타와 불장은 동일하게 daily_report 함수를 태웁니다.
    if args.mode in ['danta', 'bull']:
        generate_daily_report(args.market, args.mode)
    elif args.mode == 'dca':
        generate_weekly_dca_report(args.market)
