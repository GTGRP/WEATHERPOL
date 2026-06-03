"""
Telegram Bot Integration — Notifications + Commands.

Features:
- Send trade alerts (entry, exit, win, loss)
- Send daily summary
- Commands: /status, /positions, /balance, /pnl, /markets, /stop
- Non-blocking (runs in background thread)
"""

import time
import threading
import requests
from typing import Optional, Dict, List
from datetime import datetime, timezone

from config import Config
from logger import log


class TelegramBot:
    """Telegram bot for notifications and commands."""

    def __init__(self, position_manager=None, scanner=None):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.pm = position_manager
        self.scanner = scanner
        self._session = requests.Session()
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_id = 0

        if not self.enabled:
            log.info("Telegram: disabled (no token/chat_id set)")
        else:
            log.info(f"Telegram: enabled → chat {self.chat_id}")

    @property
    def base_url(self):
        return f"https://api.telegram.org/bot{self.token}"

    # ═══════════════════════════════════════════════════════════════
    # SEND MESSAGES
    # ═══════════════════════════════════════════════════════════════

    def send(self, text: str, parse_mode: str = 'HTML') -> bool:
        """Send a message to the configured chat."""
        if not self.enabled:
            return False
        try:
            resp = self._session.post(
                f"{self.base_url}/sendMessage",
                json={
                    'chat_id': self.chat_id,
                    'text': text,
                    'parse_mode': parse_mode,
                    'disable_web_page_preview': True,
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram send failed: {e}")
            return False

    def notify_trade(self, side: str, bucket_label: str, price: float,
                     size_usd: float, shares: float, strategy: str,
                     edge: float = 0, city: str = ''):
        """Send trade notification."""
        emoji = '🟢' if side == 'BUY' else '🔴'
        msg = (
            f"{emoji} <b>{side}</b> — {strategy.upper()}\n"
            f"📍 {city} | {bucket_label}\n"
            f"💰 ${price:.4f} × {shares:.0f} = ${size_usd:.2f}\n"
        )
        if edge > 0:
            msg += f"📊 Edge: {edge:.1%}\n"
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        msg += f"\n{mode}"
        self.send(msg)

    def notify_resolution(self, won: bool, bucket_label: str, pnl: float, city: str = ''):
        """Send resolution notification."""
        emoji = '✅' if won else '❌'
        result = 'WON' if won else 'LOST'
        msg = (
            f"{emoji} <b>RESOLVED: {result}</b>\n"
            f"📍 {city} | {bucket_label}\n"
            f"💰 PnL: ${pnl:+.2f}\n"
        )
        self.send(msg)

    def notify_redeem(self, bucket_label: str, amount: float):
        """Send redemption notification."""
        msg = (
            f"💰 <b>REDEEMED</b>\n"
            f"📍 {bucket_label}\n"
            f"💵 +${amount:.2f}\n"
        )
        self.send(msg)

    def send_status(self):
        """Send current bot status."""
        if not self.pm:
            return
        stats = self.pm.get_stats()
        open_pos = self.pm.get_open_positions()

        msg = (
            f"📊 <b>Weather Sniper Status</b>\n"
            f"{'─'*30}\n"
            f"Mode: {stats['mode']}\n"
            f"Balance: ${stats['balance']:.2f}\n"
            f"Portfolio: ${stats['portfolio_value']:.2f}\n"
            f"PnL: ${stats['total_pnl']:+.2f} ({stats['roi_pct']:+.1f}%)\n"
            f"{'─'*30}\n"
            f"Trades: {stats['total_trades']}\n"
            f"Win Rate: {stats['win_rate']:.0f}% "
            f"({stats['wins']}W / {stats['losses']}L)\n"
            f"Open: {stats['open_positions']}\n"
            f"Redeemed: ${stats['total_redeemed']:.2f}\n"
        )

        if open_pos:
            msg += f"\n<b>Open Positions:</b>\n"
            for p in open_pos[:8]:
                pnl_emoji = '📈' if p.unrealized_pnl >= 0 else '📉'
                msg += (f"  {pnl_emoji} {p.city} {p.bucket_label[:25]}\n"
                        f"    ${p.entry_price:.4f}→${p.current_price:.4f} "
                        f"({p.roi_pct:+.0f}%)\n")

        self.send(msg)

    def send_markets_summary(self):
        """Send summary of available markets."""
        if not self.scanner:
            return
        markets = self.scanner.scan_weather_markets(days_ahead=2)
        msg = f"🌤️ <b>Active Weather Markets: {len(markets)}</b>\n\n"
        
        # Group by city
        by_city: Dict[str, int] = {}
        for m in markets:
            by_city[m.city] = by_city.get(m.city, 0) + 1
        
        for city, count in sorted(by_city.items(), key=lambda x: -x[1]):
            msg += f"  📍 {city}: {count} markets\n"
        
        self.send(msg)

    def send_daily_summary(self):
        """Send end-of-day summary."""
        if not self.pm:
            return
        stats = self.pm.get_stats()
        today_positions = [p for p in self.pm.positions
                          if p.entry_time.date() == datetime.now(timezone.utc).date()]
        
        today_pnl = sum(p.pnl for p in today_positions if p.status != 'open')
        
        msg = (
            f"📅 <b>Daily Summary</b>\n"
            f"{'─'*30}\n"
            f"New trades today: {len(today_positions)}\n"
            f"Today's PnL: ${today_pnl:+.2f}\n"
            f"Total PnL: ${stats['total_pnl']:+.2f}\n"
            f"Balance: ${stats['balance']:.2f}\n"
            f"Portfolio: ${stats['portfolio_value']:.2f}\n"
            f"Win Rate: {stats['win_rate']:.0f}%\n"
        )
        self.send(msg)

    # ═══════════════════════════════════════════════════════════════
    # COMMAND HANDLER (polls for incoming commands)
    # ═══════════════════════════════════════════════════════════════

    def start_polling(self):
        """Start polling for commands in a background thread."""
        if not self.enabled:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info("Telegram command polling started")

    def stop_polling(self):
        """Stop polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)

    def _poll_loop(self):
        """Background loop to check for incoming commands."""
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                log.debug(f"Telegram poll error: {e}")
            time.sleep(3)

    def _check_updates(self):
        """Check for new Telegram messages/commands."""
        try:
            resp = self._session.get(
                f"{self.base_url}/getUpdates",
                params={'offset': self._last_update_id + 1, 'timeout': 2},
                timeout=10,
            )
            if resp.status_code != 200:
                return

            data = resp.json()
            for update in data.get('result', []):
                self._last_update_id = update['update_id']
                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = str(msg.get('chat', {}).get('id', ''))

                # Only respond to our configured chat
                if chat_id != self.chat_id:
                    continue

                self._handle_command(text)
        except Exception:
            pass

    def _handle_command(self, text: str):
        """Handle incoming bot commands."""
        cmd = text.lower().split()[0] if text else ''

        if cmd == '/status' or cmd == '/stats':
            self.send_status()
        elif cmd == '/balance' or cmd == '/bal':
            bal = self.pm.get_balance() if self.pm else 0
            self.send(f"💰 Balance: ${bal:.2f}")
        elif cmd == '/pnl':
            pnl = self.pm.get_total_pnl() if self.pm else 0
            self.send(f"📊 Total PnL: ${pnl:+.2f}")
        elif cmd == '/positions' or cmd == '/pos':
            self.send_status()
        elif cmd == '/markets':
            self.send_markets_summary()
        elif cmd == '/redeem':
            if self.pm:
                count = self.pm.redeem_all_winning()
                self.send(f"💰 Redeemed {count} positions")
        elif cmd == '/help':
            self.send(
                "🌤️ <b>Weather Sniper Commands</b>\n"
                "/status — Bot status & positions\n"
                "/balance — Current balance\n"
                "/pnl — Total profit/loss\n"
                "/positions — Open positions\n"
                "/markets — Active weather markets\n"
                "/redeem — Redeem winning positions\n"
                "/help — This message"
            )
        elif cmd.startswith('/'):
            self.send(f"❓ Unknown command. Try /help")
