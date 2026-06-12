"""
Telegram Bot Integration — Notifications + Commands.

Features:
- Send trade alerts (entry, exit, win, loss)
- Detailed redemption alerts (full market, entry/exit, profit/PnL)
- Send daily summary
- Commands: /status, /positions, /balance, /pnl, /markets, /stop
- Paginated + sortable positions view (10 per page)
- Non-blocking (runs in background thread)
"""

import html
import time
import threading
import requests
from typing import Optional, Dict, List
from datetime import datetime, timezone

from config import Config
from logger import log


class TelegramBot:
    """Telegram bot for notifications and commands."""

    PAGE_SIZE = 10
    _SORT_NAMES = {
        'pnl': 'Top PnL', 'loss': 'Biggest losers',
        'roi': 'Top ROI', 'recent': 'Most recent',
    }

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
        # Seed with already-redeemed ids so a restart doesn't re-announce the
        # whole backlog — only NEW redemptions after startup are sent.
        self._announced_redeemed = set(
            p.id for p in self.pm.positions if p.status == 'redeemed'
        ) if self.pm else set()

        if not self.enabled:
            log.info("Telegram: disabled (no token/chat_id set)")
        else:
            log.info(f"Telegram: enabled → chat {self.chat_id}")

    @property
    def base_url(self):
        return "https" + "://api.telegram.org/bot" + str(self.token)

    @staticmethod
    def _esc(s) -> str:
        """HTML-escape dynamic text so market names with &/</> don't break parse."""
        return html.escape(str(s if s is not None else ''))

    # ══════════════════════════════════════════════════════════════════
    # SEND MESSAGES
    # ══════════════════════════════════════════════════════════════════

    def send(self, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> bool:
        """Send a message to the configured chat (optionally with an inline keyboard)."""
        if not self.enabled:
            return False
        # Intercept the legacy blind "Redeemed N winning positions!" message and
        # replace it with the detailed per-position breakdown (full market, entry/
        # exit, cost/payout, PnL). The detailed header starts with "<b>REDEEMED",
        # so it never matches this guard and there is no recursion.
        stripped = text.strip()
        if stripped.startswith('\U0001F4B0 Redeemed ') and stripped.endswith('positions!'):
            self.notify_redeems_recent()
            return True
        try:
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            resp = self._session.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram send failed: {e}")
            return False

    def _edit(self, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message (used to refresh panels in place)."""
        if not self.enabled:
            return False
        try:
            payload = {
                'chat_id': self.chat_id, 'message_id': message_id,
                'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            r = self._session.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.debug(f"Telegram edit failed: {e}")
            return False

    def _answer_callback(self, callback_id: str, text: str = ''):
        try:
            self._session.post(f"{self.base_url}/answerCallbackQuery",
                               json={'callback_query_id': callback_id, 'text': text},
                               timeout=10)
        except Exception:
            pass

    def notify_trade(self, side: str, bucket_label: str, price: float,
                     size_usd: float, shares: float, strategy: str,
                     edge: float = 0, city: str = ''):
        """Send trade notification."""
        emoji = '🟢' if side == 'BUY' else '🔴'
        msg = (
            f"{emoji} <b>{side}</b> — {self._esc(strategy.upper())}\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 ${price:.4f} × {shares:.0f} = ${size_usd:.2f}\n"
        )
        if edge > 0:
            msg += f"📊 Edge: {edge:.1%}\n"
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        msg += f"\n{mode}"
        self.send(msg)

    def notify_resolution(self, won: bool, bucket_label: str, pnl: float, city: str = ''):
        """Send simple resolution notification (kept for compatibility)."""
        emoji = '✅' if won else '❌'
        result = 'WON' if won else 'LOST'
        msg = (
            f"{emoji} <b>RESOLVED: {result}</b>\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 PnL: ${pnl:+.2f}\n"
        )
        self.send(msg)

    def notify_close(self, pos):
        """Send a close/resolution alert for ANY closed position — stop-loss,
        take-profit, trailing-stop, flip/thesis exit, or won/lost resolution.

        Wired via PositionManager._notify_close (risk-trigger & resolution
        closes) and called directly by the dashboard for flip/thesis exits
        (whose reason is relabeled 'manual' after close, so the PM hook skips
        them to avoid a double-notify). Fully defensive — never raises."""
        try:
            reason = getattr(pos, 'exit_reason', '') or ''
            status = getattr(pos, 'status', '') or ''
            pnl = getattr(pos, 'pnl', 0.0) or 0.0
            roi = getattr(pos, 'roi_pct', 0.0) or 0.0
            if status == 'won':
                head = '✅ <b>RESOLVED WON</b>'
            elif status == 'lost':
                head = '❌ <b>RESOLVED LOST</b>'
            else:
                head = {
                    'take_profit': '🎯 <b>TAKE PROFIT</b>',
                    'stop_loss': '🛑 <b>STOP LOSS</b>',
                    'trailing_stop': '📉 <b>TRAILING STOP</b>',
                    'flip_timeout': '⏲️ <b>FLIP book-or-cut</b>',
                    'thesis_invalidated': '🚫 <b>THESIS EXIT</b>',
                    'manual': '🔴 <b>SOLD</b>',
                }.get(reason, '🔴 <b>SOLD</b>')
            entry = getattr(pos, 'entry_price', 0.0) or 0.0
            exit_px = getattr(pos, 'exit_price', None)
            if exit_px is None:
                exit_px = getattr(pos, 'current_price', 0.0) or 0.0
            shares = getattr(pos, 'shares', 0.0) or 0.0
            name = self._esc(getattr(pos, 'bucket_label', '') or getattr(pos, 'market_title', ''))
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            msg = (
                f"{head} — {self._esc(getattr(pos, 'strategy', ''))}\n"
                f"📍 {self._esc(getattr(pos, 'city', ''))} | {name}\n"
                f"💵 entry ${entry:.4f} → exit ${exit_px:.4f} | {shares:.0f}sh\n"
                f"📊 PnL ${pnl:+.2f} ({roi:+.0f}%)\n"
                f"{mode}"
            )
            self.send(msg)
        except Exception as e:
            log.debug(f"notify_close failed: {e}")

    def notify_redeems_recent(self):
        """Find positions that have newly become 'redeemed' since the last call
        and announce them in full detail. Self-discovers from the position
        manager so the dashboard doesn't need to pass anything."""
        if not self.pm:
            return
        fresh = [p for p in self.pm.positions
                 if p.status == 'redeemed' and p.id not in self._announced_redeemed]
        for p in fresh:
            self._announced_redeemed.add(p.id)
        if fresh:
            self.notify_redeems(fresh)

    def notify_redeems(self, positions: List):
        """Detailed redemption notification — one block per redeemed position with
        the full market name, entry/exit price, cost/payout and realized PnL."""
        if not positions:
            return
        payout_total = sum(p.shares * 1.0 for p in positions)
        pnl_total = sum(p.pnl for p in positions)
        n = len(positions)
        header = (
            f"💰 <b>REDEEMED {n} winning position{'s' if n != 1 else ''}</b>\n"
            f"   payout +${payout_total:.2f} | realized PnL ${pnl_total:+.2f}\n"
        )
        blocks = []
        for p in positions:
            exit_px = p.exit_price if p.exit_price is not None else 1.0
            payout = p.shares * 1.0
            name = self._esc(p.bucket_label or p.market_title)
            blocks.append(
                f"\n✅ <b>{self._esc(p.city)}</b>  ({self._esc(p.strategy)})\n"
                f"   {name}\n"
                f"   entry ${p.entry_price:.4f} → exit ${exit_px:.4f} | {p.shares:.0f}sh\n"
                f"   cost ${p.cost_usd:.2f} → payout ${payout:.2f} | "
                f"PnL ${p.pnl:+.2f} ({p.roi_pct:+.0f}%)\n"
            )
        # Respect Telegram's ~4096-char message cap — chunk if necessary.
        msg = header
        for b in blocks:
            if len(msg) + len(b) > 3900:
                self.send(msg)
                msg = ''
            msg += b
        if msg:
            self.send(msg)

    def notify_redeem(self, bucket_label: str, amount: float):
        """Legacy single-redeem notification (kept for compatibility)."""
        msg = (
            f"💰 <b>REDEEMED</b>\n"
            f"📍 {self._esc(bucket_label)}\n"
            f"💵 +${amount:.2f}\n"
        )
        self.send(msg)

    # ══════════════════════════════════════════════════════════════════
    # POSITIONS VIEW (paginated + sortable)
    # ══════════════════════════════════════════════════════════════════

    def _sorted_open(self, sort_key: str) -> List:
        open_pos = self.pm.get_open_positions() if self.pm else []
        if sort_key == 'pnl':
            return sorted(open_pos, key=lambda p: p.unrealized_pnl, reverse=True)
        if sort_key == 'loss':
            return sorted(open_pos, key=lambda p: p.unrealized_pnl)
        if sort_key == 'roi':
            return sorted(open_pos, key=lambda p: p.roi_pct, reverse=True)
        # 'recent' (default): newest entry first
        return sorted(open_pos, key=lambda p: p.entry_time, reverse=True)

    def _fmt_position(self, p, idx: int) -> str:
        pe = '🟢' if p.unrealized_pnl >= 0 else '🔴'
        lock = ' 🔒' if getattr(p, 'preclose_locked', False) else ''
        stale = ' ~stale' if getattr(p, 'current_price_stale', False) else ''
        name = self._esc(p.bucket_label or p.market_title)
        return (
            f"{idx}. {pe} <b>{self._esc(p.city)}</b>  "
            f"${p.unrealized_pnl:+.2f} ({p.roi_pct:+.0f}%){lock}{stale}\n"
            f"   {name}\n"
            f"   entry ${p.entry_price:.4f} → ${p.current_price:.4f} | "
            f"{p.shares:.0f}sh | cost ${p.cost_usd:.2f} | {self._esc(p.strategy)}\n\n"
        )

    def _positions_view(self, page: int = 0, sort: str = 'recent',
                        with_summary: bool = False):
        """Build (text, inline_keyboard) for a page of open positions."""
        positions = self._sorted_open(sort)
        total = len(positions)
        pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * self.PAGE_SIZE
        chunk = positions[start:start + self.PAGE_SIZE]

        text = ''
        if with_summary and self.pm:
            s = self.pm.get_stats()
            text += (
                f"📊 <b>Weather Sniper Status</b>\n"
                f"Mode: {s['mode']} | Balance: ${s['balance']:.2f}\n"
                f"PnL: ${s['total_pnl']:+.2f} ({s['roi_pct']:+.1f}%) | "
                f"WR: {s['win_rate']:.0f}% ({s['wins']}W/{s['losses']}L)\n"
                f"Trades: {s['total_trades']} | Open: {s['open_positions']} | "
                f"Redeemed: ${s['total_redeemed']:.2f}\n"
                f"{'─'*28}\n"
            )
        sort_name = self._SORT_NAMES.get(sort, sort)
        shown_to = start + len(chunk)
        text += (f"<b>Open positions {start + 1}–{shown_to} of {total}</b> "
                 f"· sorted: {sort_name}\n\n")
        if not chunk:
            text += "No open positions.\n"
        else:
            for i, p in enumerate(chunk, start=start + 1):
                text += self._fmt_position(p, i)

        sm = '1' if with_summary else '0'
        nav = []
        if page > 0:
            nav.append({'text': '⬅️ Prev', 'callback_data': f"pos:{page-1}:{sort}:{sm}"})
        nav.append({'text': f"{page+1}/{pages}", 'callback_data': 'noop'})
        if page < pages - 1:
            nav.append({'text': 'Next ➡️', 'callback_data': f"pos:{page+1}:{sort}:{sm}"})
        dot = lambda k: ('• ' if sort == k else '')
        sort_row = [
            {'text': dot('pnl') + '💰 PnL', 'callback_data': f"pos:0:pnl:{sm}"},
            {'text': dot('loss') + '📉 Losses', 'callback_data': f"pos:0:loss:{sm}"},
            {'text': dot('roi') + '📈 ROI', 'callback_data': f"pos:0:roi:{sm}"},
            {'text': dot('recent') + '🕒 Recent', 'callback_data': f"pos:0:recent:{sm}"},
        ]
        return text, {'inline_keyboard': [nav, sort_row]}

    def send_positions(self, page: int = 0, sort: str = 'recent',
                       with_summary: bool = False, edit_message_id: int = None):
        if not self.pm:
            return
        text, kb = self._positions_view(page, sort, with_summary)
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def send_status(self):
        """Status = summary + first page of open positions (paginated/sortable)."""
        if not self.pm:
            return
        self.send_positions(page=0, sort='recent', with_summary=True)

    def send_markets_summary(self):
        """Send summary of available markets."""
        if not self.scanner:
            return
        markets = self.scanner.scan_weather_markets(days_ahead=2)
        msg = f"🌤️ <b>Active Weather Markets: {len(markets)}</b>\n\n"
        by_city: Dict[str, int] = {}
        for m in markets:
            by_city[m.city] = by_city.get(m.city, 0) + 1
        for city, count in sorted(by_city.items(), key=lambda x: -x[1]):
            msg += f"  📍 {self._esc(city)}: {count} markets\n"
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

    # ══════════════════════════════════════════════════════════════════
    # COMMAND HANDLER (polls for incoming commands)
    # ══════════════════════════════════════════════════════════════════

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
            try:
                self.notify_redeems_recent()
            except Exception as e:
                log.debug(f"redeem announce error: {e}")
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

                cb = update.get('callback_query')
                if cb:
                    cb_chat = str(cb.get('message', {}).get('chat', {}).get('id', ''))
                    if cb_chat == self.chat_id:
                        self._handle_callback(
                            cb.get('data', ''), cb.get('id', ''),
                            cb.get('message', {}).get('message_id'),
                        )
                    continue

                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = str(msg.get('chat', {}).get('id', ''))

                if chat_id != self.chat_id:
                    continue

                self._handle_command(text)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    # SETTINGS PANEL (live tunables + tick-box toggles)
    # ══════════════════════════════════════════════════════════════════

    _LABELS = {
        'TRADING_ENABLED': 'Trading', 'SNIPER_ENABLED': 'Sniper',
        'SPREAD_ENABLED': 'Spread', 'CONFIDENT_ENABLED': 'Confident',
        'STABILITY_ENABLED': 'Stability', 'LIQUIDITY_GUARD_ENABLED': 'LiqGuard',
        'LIQUIDITY_STRICT_BLOCK': 'LiqStrict', 'GRADE_SIZING_ENABLED': 'GradeSize',
        'SKIP_DECIDED_MARKETS': 'SkipDecided', 'ML_ENABLED': 'ML',
    }

    def _settings_view(self):
        """Build (text, inline_keyboard) for the settings panel."""
        from bot import settings_store
        bools, nums = settings_store.snapshot()

        text = "⚙️ <b>Bot Settings</b> — tap to change\n"
        text += f"Trading: {'🟢 ON' if bools.get('TRADING_ENABLED') else '🔴 OFF'}\n\n"
        text += "<b>Strategies / toggles</b> (tap to flip)\n"
        text += "<b>Gates</b> (tap ➖/➕)\n"
        for k, v in nums.items():
            text += f"  {k} = <b>{v}</b>\n"

        rows = []
        bkeys = settings_store.BOOL_KEYS
        for i in range(0, len(bkeys), 2):
            row = []
            for k in bkeys[i:i + 2]:
                on = bools.get(k)
                label = self._LABELS.get(k, k)
                row.append({'text': f"{'✅' if on else '❌'} {label}",
                            'callback_data': f"tg:{k}"})
            rows.append(row)
        for k in settings_store.NUM_KEYS:
            v = nums.get(k)
            rows.append([
                {'text': '➖', 'callback_data': f"dn:{k}"},
                {'text': f"{k}={v}", 'callback_data': 'noop'},
                {'text': '➕', 'callback_data': f"up:{k}"},
            ])
        return text, {'inline_keyboard': rows}

    def send_settings(self, edit_message_id: int = None):
        text, kb = self._settings_view()
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def _handle_callback(self, data: str, callback_id: str, message_id):
        from bot import settings_store
        if not data or data == 'noop':
            self._answer_callback(callback_id)
            return

        # Positions pager/sorter: "pos:<page>:<sort>:<with_summary>"
        if data.startswith('pos:'):
            try:
                _, page_s, sort_key, sm = data.split(':')
                page = int(page_s)
            except (ValueError, IndexError):
                self._answer_callback(callback_id)
                return
            self._answer_callback(callback_id)
            self.send_positions(page=page, sort=sort_key,
                                with_summary=(sm == '1'),
                                edit_message_id=message_id)
            return

        try:
            action, key = data.split(':', 1)
        except ValueError:
            self._answer_callback(callback_id)
            return
        ok, msg = False, 'no change'
        if action == 'tg':
            ok, msg = settings_store.toggle(key)
        elif action == 'up':
            ok, msg = settings_store.bump(key, +1)
        elif action == 'dn':
            ok, msg = settings_store.bump(key, -1)
        self._answer_callback(callback_id, msg)
        if ok and message_id is not None:
            self.send_settings(edit_message_id=message_id)

    def _handle_command(self, text: str):
        """Handle incoming bot commands."""
        cmd = text.lower().split()[0] if text else ''
        parts = text.split()

        if cmd == '/start' or cmd == '/resume':
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', True)
            self.send("🟢 <b>Trading ENABLED</b> — the bot will place new trades.")
        elif cmd == '/stop' or cmd == '/pause':
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', False)
            self.send("🔴 <b>Trading DISABLED</b> — monitoring & resolving only, no new buys.")
        elif cmd == '/settings' or cmd == '/config':
            self.send_settings()
        elif cmd == '/set':
            from bot import settings_store
            if len(parts) >= 3:
                ok, msg = settings_store.set_value(parts[1], parts[2])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/set KEY VALUE</code>  e.g. <code>/set BASKET_MAX_COST 0.80</code>")
        elif cmd == '/toggle':
            from bot import settings_store
            if len(parts) >= 2:
                ok, msg = settings_store.toggle(parts[1])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/toggle KEY</code>  e.g. <code>/toggle SNIPER_ENABLED</code>")
        elif cmd == '/status' or cmd == '/stats':
            self.send_status()
        elif cmd == '/balance' or cmd == '/bal':
            bal = self.pm.get_balance() if self.pm else 0
            self.send(f"💰 Balance: ${bal:.2f}")
        elif cmd == '/pnl':
            pnl = self.pm.get_total_pnl() if self.pm else 0
            self.send(f"📊 Total PnL: ${pnl:+.2f}")
        elif cmd == '/positions' or cmd == '/pos':
            self.send_positions(page=0, sort='recent', with_summary=False)
        elif cmd == '/markets':
            self.send_markets_summary()
        elif cmd == '/redeem':
            if self.pm:
                count = self.pm.redeem_all_winning()
                # redeem_all_winning may return a count (int) or a list.
                n = len(count) if isinstance(count, list) else count
                self.notify_redeems_recent()
                self.send(f"💰 Redeemed {n} positions")
        elif cmd == '/help':
            self.send(
                "🌤️ <b>Weather Sniper Commands</b>\n"
                "<b>/start</b> — enable trading\n"
                "<b>/stop</b> — disable trading (monitor only)\n"
                "<b>/settings</b> — toggle strategies & tune gates (buttons)\n"
                "/set KEY VALUE — set a gate, e.g. /set BASKET_MAX_COST 0.80\n"
                "/toggle KEY — flip a toggle, e.g. /toggle SNIPER_ENABLED\n"
                "/status — summary + positions (paged, sortable)\n"
                "/balance — current balance\n"
                "/pnl — total profit/loss\n"
                "/positions — open positions (10/page; sort by PnL/Losses/ROI/Recent)\n"
                "/markets — active weather markets\n"
                "/redeem — redeem winning positions\n"
                "/help — this message"
            )
        elif cmd.startswith('/'):
            self.send(f"❓ Unknown command. Try /help")
