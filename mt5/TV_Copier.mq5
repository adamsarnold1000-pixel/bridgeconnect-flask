//+------------------------------------------------------------------+
//|                                                TV_Copier.mq5     |
//|                       TradingView → MT5 Bridge EA                |
//|         Polls Flask server for pending trades, executes them     |
//|         with entry, SL, TP, and exact lot size from TradingView  |
//+------------------------------------------------------------------+
#property copyright "TV-MT5 Bridge"
#property link      ""
#property version   "3.00"

#include <Trade\Trade.mqh>
#include <Trade\SymbolInfo.mqh>

CTrade trade;
CSymbolInfo symbolInfo;

//--- Server connection
input string ServerURL      = "http://127.0.0.1:5000"; // Bridge server URL
input int    PollIntervalMs = 500;                      // Poll interval (ms)
input bool   EnableLogging  = true;                     // Enable console logging

//--- Trade execution
input int    MagicNumber    = 99999;                    // Magic number for trades
input int    MaxRetries     = 5;                        // Max retry attempts per trade
input int    RetryDelayMs   = 100;                      // Delay between retries (ms)

//--- Risk management (fallback if TradingView size is missing)
input double FallbackLots   = 0.01;                     // Fallback lot size if TV sends 0
input int    MaxDailyTrades = 50;                       // Max daily trades (safety cap)

// ====================================================================
// GLOBALS
// ====================================================================
int    dailyTradeCount = 0;
datetime lastResetTime;

// Track open positions for manual-close detection
int    g_trackedTradeIds[];
ulong  g_trackedTickets[];

// ====================================================================
// INIT / DEINIT
// ====================================================================
int OnInit() {
   EventSetMillisecondTimer(PollIntervalMs);
   trade.SetExpertMagicNumber(MagicNumber);
   PingServer();
   lastResetTime = TimeCurrent();
   if(EnableLogging) Print("[INIT] TV_Copier v3.00 started. Polling every ", PollIntervalMs, "ms → ", ServerURL);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
   if(EnableLogging) Print("[DEINIT] TV_Copier stopped. Reason=", reason);
}

// ====================================================================
// PING — heartbeat to register this MT5 account with the server
// ====================================================================
void PingServer() {
   string reqHeaders = "Content-Type: application/json\r\n";
   string respHeaders;
   char post[], result[];

   string accountId = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string name      = AccountInfoString(ACCOUNT_NAME);
   string server    = AccountInfoString(ACCOUNT_SERVER);
   double balance   = AccountInfoDouble(ACCOUNT_BALANCE);
   string symbol    = Symbol();

   string type;
   int tradeMode = (int)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(tradeMode == ACCOUNT_TRADE_MODE_DEMO)         type = "DEMO";
   else if(tradeMode == ACCOUNT_TRADE_MODE_CONTEST)  type = "CONTEST";
   else                                               type = "LIVE";

   string json = "{\"account_id\":\"" + accountId + "\",\"name\":\"" + name +
                 "\",\"server\":\"" + server + "\",\"balance\":" +
                 DoubleToString(balance, 2) + ",\"type\":\"" + type +
                 "\",\"symbol\":\"" + symbol + "\"}";

   StringToCharArray(json, post, 0, WHOLE_ARRAY, CP_UTF8);
   int postLen = ArraySize(post);
   if(postLen > 0 && post[postLen - 1] == 0) ArrayResize(post, postLen - 1);

   int res = WebRequest("POST", ServerURL + "/api/mt5/ping", reqHeaders, 5000, post, result, respHeaders);

   if(res == -1) {
      int err = GetLastError();
      Print("[PING] FAILED — WebRequest blocked. Error=", err,
            " | Fix: MT5 → Tools → Options → Expert Advisors → Allow WebRequest → add: ", ServerURL);
   } else if(res != 200) {
      if(EnableLogging) Print("[PING] HTTP ", res);
   }
   // success: silent unless verbose
}

// ====================================================================
// JSON PARSER — extract values from trade JSON
// ====================================================================
string JsonExtractRaw(const string json, int afterColon) {
   int len = StringLen(json);
   int i = afterColon;
   while(i < len && (StringGetCharacter(json, i) == ' ' || StringGetCharacter(json, i) == '\t')) i++;
   if(i >= len) return "";
   ushort first = StringGetCharacter(json, i);
   if(first == '"') {
      int start = i + 1;
      int end = StringFind(json, "\"", start);
      if(end < 0) return "";
      return StringSubstr(json, start, end - start);
   }
   int start = i;
   while(i < len) {
      ushort c = StringGetCharacter(json, i);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\t' || c == '\n' || c == '\r') break;
      i++;
   }
   return StringSubstr(json, start, i - start);
}

bool ParseTradeJson(const string json, int& id, string& symbol, string& action,
                    double& sl, double& tp, double& price, double& riskValue) {
   id = 0; symbol = ""; action = ""; sl = 0; tp = 0; price = 0; riskValue = 0;

   int pos;

   // id
   pos = StringFind(json, "\"id\":");
   if(pos >= 0) id = (int)StringToInteger(JsonExtractRaw(json, pos + 5));

   // symbol / ticker
   pos = StringFind(json, "\"symbol\":");
   if(pos < 0) pos = StringFind(json, "\"ticker\":");
   if(pos >= 0) {
      symbol = JsonExtractRaw(json, pos + (StringFind(json, ":", pos) - pos + 1));
      int colonPos = StringFind(symbol, ":");
      if(colonPos >= 0) symbol = StringSubstr(symbol, colonPos + 1);
   }

   // action / event
   pos = StringFind(json, "\"action\":");
   if(pos < 0) pos = StringFind(json, "\"event\":");
   if(pos >= 0) {
      action = JsonExtractRaw(json, pos + (StringFind(json, ":", pos) - pos + 1));
      StringToUpper(action);
   }

   // price
   pos = StringFind(json, "\"price\":");
   if(pos >= 0) price = StringToDouble(JsonExtractRaw(json, pos + 8));

   // sl
   pos = StringFind(json, "\"sl\":");
   if(pos >= 0) sl = StringToDouble(JsonExtractRaw(json, pos + 5));

   // tp
   pos = StringFind(json, "\"tp\":");
   if(pos >= 0) tp = StringToDouble(JsonExtractRaw(json, pos + 5));

   // risk_value (= lot size from TradingView)
   pos = StringFind(json, "\"risk_value\":");
   if(pos >= 0) riskValue = StringToDouble(JsonExtractRaw(json, pos + 13));

   return (id > 0 && symbol != "" && action != "");
}

// ====================================================================
// TRADE EXECUTION — with retry, SL/TP placement, lot normalization
// ====================================================================

// Normalize lot size to broker constraints
double NormalizeLots(const string sym, double lots) {
   if(!symbolInfo.Name(sym)) return lots;

   double minLot  = symbolInfo.LotsMin();
   double maxLot  = symbolInfo.LotsMax();
   double lotStep = symbolInfo.LotsStep();

   if(lots < minLot) lots = minLot;
   if(lots > maxLot) lots = maxLot;
   if(lotStep > 0)   lots = MathFloor(lots / lotStep) * lotStep;

   return NormalizeDouble(lots, 2);
}

// Execute a market order at current price
bool ExecuteTradeWithRetry(const string symbol, const bool isBuy, double volume,
                           double sl, double tp, ulong& ticketId) {
   int retryCount = 0;
   ticketId = 0;
   volume = NormalizeLots(symbol, volume);

   while(retryCount < MaxRetries) {
      bool success = false;

      // Open at market — SL/TP attached via PositionModify after fill
      // to avoid "invalid stops" (error 10016) on some brokers
      if(isBuy)
         success = trade.Buy(volume, symbol, 0, 0, 0, "TV Signal");
      else
         success = trade.Sell(volume, symbol, 0, 0, 0, "TV Signal");

      if(success) {
         ticketId = trade.ResultOrder();
         if(EnableLogging) Print("[EXEC] Trade OK. Ticket=", ticketId, " Vol=", volume);

         // Now attach SL/TP to the position
         if(sl > 0 || tp > 0)
            AttachStopLevels(symbol, ticketId, sl, tp, isBuy);

         return true;
      }

      int error = GetLastError();
      if(EnableLogging) Print("[EXEC] Attempt ", retryCount + 1, "/", MaxRetries, " failed. Error=", error);
      retryCount++;
      if(retryCount < MaxRetries) Sleep(RetryDelayMs);
   }

   if(EnableLogging) Print("[EXEC] FAILED after ", MaxRetries, " attempts");
   return false;
}

// Attach SL and/or TP to an open position after market fill
void AttachStopLevels(const string symbol, ulong orderTicket, double sl, double tp, bool isBuy) {
   // Wait briefly for the position to appear in MT5's position list
   Sleep(200);

   // Find the position ticket for this order
   ulong posTicket = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
         posTicket = t;
         break;  // most recent position on this symbol
      }
   }

   if(posTicket == 0) {
      if(EnableLogging) Print("[SL/TP] Could not find position to modify for ", symbol);
      return;
   }

   if(!PositionSelectByTicket(posTicket)) return;

   // Normalize SL/TP to tick size
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) tickSize = SymbolInfoDouble(symbol, SYMBOL_POINT);

   if(sl > 0 && tickSize > 0) sl = MathRound(sl / tickSize) * tickSize;
   if(tp > 0 && tickSize > 0) tp = MathRound(tp / tickSize) * tickSize;

   // Validate SL/TP direction
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   if(isBuy) {
      // BUY: SL must be below entry, TP must be above
      if(sl > 0 && sl >= openPrice) {
         if(EnableLogging) Print("[SL/TP] WARNING: BUY SL=", sl, " >= entry=", openPrice, " — skipping SL");
         sl = 0;
      }
      if(tp > 0 && tp <= openPrice) {
         if(EnableLogging) Print("[SL/TP] WARNING: BUY TP=", tp, " <= entry=", openPrice, " — skipping TP");
         tp = 0;
      }
   } else {
      // SELL: SL must be above entry, TP must be below
      if(sl > 0 && sl <= openPrice) {
         if(EnableLogging) Print("[SL/TP] WARNING: SELL SL=", sl, " <= entry=", openPrice, " — skipping SL");
         sl = 0;
      }
      if(tp > 0 && tp >= openPrice) {
         if(EnableLogging) Print("[SL/TP] WARNING: SELL TP=", tp, " >= entry=", openPrice, " — skipping TP");
         tp = 0;
      }
   }

   if(sl <= 0 && tp <= 0) return;  // nothing to set

   // Check broker minimum stop distance
   int stopsLevel = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double minDist = stopsLevel * point;

   if(sl > 0 && MathAbs(openPrice - sl) < minDist) {
      if(EnableLogging) Print("[SL/TP] SL too close (", MathAbs(openPrice - sl), " < minDist=", minDist, ") — skipping SL");
      sl = 0;
   }
   if(tp > 0 && MathAbs(openPrice - tp) < minDist) {
      if(EnableLogging) Print("[SL/TP] TP too close (", MathAbs(openPrice - tp), " < minDist=", minDist, ") — skipping TP");
      tp = 0;
   }

   if(sl <= 0 && tp <= 0) return;

   // Modify position to add SL/TP
   bool modified = trade.PositionModify(posTicket, sl > 0 ? sl : 0, tp > 0 ? tp : 0);
   if(modified) {
      if(EnableLogging) Print("[SL/TP] Set on ticket=", posTicket, " SL=", sl, " TP=", tp);
   } else {
      int err = GetLastError();
      if(EnableLogging) Print("[SL/TP] Modify FAILED for ticket=", posTicket, " Error=", err,
                              " SL=", sl, " TP=", tp);
   }
}

// ====================================================================
// CLOSE HELPERS
// ====================================================================

// Close all positions with our magic number on the given symbol
bool CloseAllByMagic(const string symbol) {
   bool allClosed = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
         if(!trade.PositionClose(ticket)) {
            if(EnableLogging) Print("[CLOSE] Failed to close ticket=", ticket);
            allClosed = false;
         }
      }
   }
   return allClosed;
}

// Close a percentage of the open volume
bool ClosePartialByMagic(const string symbol, double percentToClose) {
   if(percentToClose <= 0)   return false;
   if(percentToClose >= 100) return CloseAllByMagic(symbol);

   bool allOk = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {

         double currentVol = PositionGetDouble(POSITION_VOLUME);
         double closeVol   = NormalizeDouble(currentVol * percentToClose / 100.0, 2);

         double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
         double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
         if(closeVol < minLot) closeVol = minLot;
         closeVol = MathFloor(closeVol / lotStep) * lotStep;
         closeVol = NormalizeDouble(closeVol, 2);

         if(closeVol >= currentVol) {
            if(!trade.PositionClose(ticket)) { allOk = false; }
         } else {
            if(!trade.PositionClosePartial(ticket, closeVol)) { allOk = false; }
            else if(EnableLogging) Print("[PARTIAL] Closed ", closeVol, " of ", currentVol, " on ticket=", ticket);
         }
      }
   }
   return allOk;
}

// ====================================================================
// HEDGE / DUPLICATE GUARDS
// ====================================================================

bool DetectAndCloseHedge(const string symbol, bool isBuy) {
   bool foundOpposite = false;
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
         ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
         bool posIsBuy = (posType == POSITION_TYPE_BUY);
         if(posIsBuy != isBuy) { foundOpposite = true; break; }
      }
   }
   if(foundOpposite) {
      if(EnableLogging) Print("[HEDGE] Opposing position on ", symbol, " — closing ALL.");
      CloseAllByMagic(symbol);
   }
   return foundOpposite;
}

bool HasExistingPosition(const string symbol, bool isBuy) {
   ENUM_POSITION_TYPE wantType = isBuy ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         PositionGetString(POSITION_SYMBOL) == symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber &&
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == wantType) {
         return true;
      }
   }
   return false;
}

// Continuous hedge check — catches race conditions
void CheckForHedgedPositions() {
   int posTotal = PositionsTotal();
   if(posTotal < 2) return;

   string symbols[];
   int symbolCount = 0;
   for(int i = 0; i < posTotal; i++) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
         string sym = PositionGetString(POSITION_SYMBOL);
         bool found = false;
         for(int s = 0; s < symbolCount; s++) {
            if(symbols[s] == sym) { found = true; break; }
         }
         if(!found) {
            ArrayResize(symbols, symbolCount + 1);
            symbols[symbolCount] = sym;
            symbolCount++;
         }
      }
   }

   for(int s = 0; s < symbolCount; s++) {
      bool hasBuy = false, hasSell = false;
      for(int i = 0; i < PositionsTotal(); i++) {
         ulong ticket = PositionGetTicket(i);
         if(PositionSelectByTicket(ticket) &&
            PositionGetString(POSITION_SYMBOL) == symbols[s] &&
            (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
            ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
            if(posType == POSITION_TYPE_BUY)  hasBuy  = true;
            if(posType == POSITION_TYPE_SELL) hasSell = true;
         }
      }
      if(hasBuy && hasSell) {
         if(EnableLogging) Print("[HEDGE] BUY + SELL on ", symbols[s], " — closing ALL.");
         CloseAllByMagic(symbols[s]);
      }
   }
}

// ====================================================================
// MANUAL CLOSE DETECTION
// ====================================================================

void TrackPosition(int tradeId, ulong ticketId) {
   int sz = ArraySize(g_trackedTradeIds);
   ArrayResize(g_trackedTradeIds, sz + 1);
   ArrayResize(g_trackedTickets,  sz + 1);
   g_trackedTradeIds[sz] = tradeId;
   g_trackedTickets[sz]  = ticketId;
}

void ReportManualClose(int tradeId, ulong ticketId, double profit) {
   string reqHeaders = "Content-Type: application/json\r\n";
   string respHeaders;
   char post[], result[];

   string accountId = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string json = "{\"trade_id\":" + IntegerToString(tradeId) +
                 ",\"ticket_id\":" + IntegerToString((long)ticketId) +
                 ",\"profit\":" + DoubleToString(profit, 2) +
                 ",\"account_id\":\"" + accountId + "\"}";

   StringToCharArray(json, post, 0, WHOLE_ARRAY, CP_UTF8);
   int postLen = ArraySize(post);
   if(postLen > 0 && post[postLen - 1] == 0) ArrayResize(post, postLen - 1);

   WebRequest("POST", ServerURL + "/api/mt5/position-closed", reqHeaders, 5000, post, result, respHeaders);
   if(EnableLogging) Print("[MANUAL_CLOSE] trade_id=", tradeId, " ticket=", ticketId, " P&L=", DoubleToString(profit, 2));
}

void CheckManuallyClosedPositions() {
   int sz = ArraySize(g_trackedTradeIds);
   if(sz == 0) return;

   for(int i = sz - 1; i >= 0; i--) {
      ulong ticket = g_trackedTickets[i];
      bool stillOpen = false;
      for(int p = 0; p < PositionsTotal(); p++) {
         if(PositionGetTicket(p) == ticket) { stillOpen = true; break; }
      }
      if(!stillOpen) {
         double closeProfit = 0;
         if(HistorySelect(0, TimeCurrent() + 60)) {
            int dealsN = HistoryDealsTotal();
            for(int di = 0; di < dealsN; di++) {
               ulong dTicket = HistoryDealGetTicket(di);
               if((ulong)HistoryDealGetInteger(dTicket, DEAL_POSITION_ID) == ticket &&
                  HistoryDealGetInteger(dTicket, DEAL_ENTRY) == DEAL_ENTRY_OUT) {
                  closeProfit += HistoryDealGetDouble(dTicket, DEAL_PROFIT)
                               + HistoryDealGetDouble(dTicket, DEAL_SWAP)
                               + HistoryDealGetDouble(dTicket, DEAL_COMMISSION);
               }
            }
         }
         ReportManualClose(g_trackedTradeIds[i], ticket, closeProfit);
         ArrayRemove(g_trackedTradeIds, i, 1);
         ArrayRemove(g_trackedTickets,  i, 1);
      }
   }
}

// ====================================================================
// CONFIRM — report execution result back to server
// ====================================================================
void ConfirmTrade(int tradeId, string status, double profit, ulong ticketId, string errorMsg = "") {
   string reqHeaders = "Content-Type: application/json\r\n";
   string respHeaders;
   char post[], result[];

   string json = "{\"id\":" + IntegerToString(tradeId) +
                 ",\"status\":\"" + status +
                 "\",\"profit\":" + DoubleToString(profit, 2) +
                 ",\"ticket_id\":" + IntegerToString((long)ticketId);
   if(StringLen(errorMsg) > 0) {
      StringReplace(errorMsg, "\\", "\\\\");
      StringReplace(errorMsg, "\"", "\\\"");
      json += ",\"error_message\":\"" + errorMsg + "\"";
   }
   json += "}";

   StringToCharArray(json, post, 0, WHOLE_ARRAY, CP_UTF8);
   int postLen = ArraySize(post);
   if(postLen > 0 && post[postLen - 1] == 0) ArrayResize(post, postLen - 1);

   int res = WebRequest("POST", ServerURL + "/api/trades/confirm", reqHeaders, 5000, post, result, respHeaders);
   if(res != 200 && EnableLogging)
      Print("[CONFIRM] FAILED for trade #", tradeId, " HTTP=", res);
   else if(EnableLogging)
      Print("[CONFIRM] Trade #", tradeId, " → ", status, " (ticket=", ticketId, ")");
}

// ====================================================================
// TIMER — main polling loop
// ====================================================================
void OnTimer() {
   // Detect manual closes
   CheckManuallyClosedPositions();

   // Continuous hedge guard
   CheckForHedgedPositions();

   // Ping every ~5 seconds (every 10 timer ticks at 500ms)
   static int tickCount = 0;
   tickCount++;
   if(tickCount >= 10) {
      PingServer();
      tickCount = 0;
   }

   // Reset daily trade count at midnight
   if(TimeCurrent() - lastResetTime >= 86400) {
      dailyTradeCount = 0;
      lastResetTime = TimeCurrent();
   }

   // Daily trade cap (safety — does not block CLOSE signals)
   // Enforced per-action below, not by skipping the poll

   // ── Poll for pending trades ─────────────────────────────────────
   string cookie = NULL, headers;
   char post[], result[];
   string accountId = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string url = ServerURL + "/api/trades/pending?account_id=" + accountId;

   int res = WebRequest("GET", url, cookie, NULL, 5000, post, 0, result, headers);

   if(res != 200) {
      if(EnableLogging && res != 0) Print("[POLL] HTTP ", res);
      return;
   }

   string json = CharArrayToString(result);

   // Quick check: does the response contain any trades?
   if(StringFind(json, "\"id\":") < 0) return;

   if(EnableLogging) Print("[POLL] Received: ", json);

   // Parse the trades array
   int arrayStart = StringFind(json, "[");
   if(arrayStart < 0) return;
   int arrayEnd = StringFind(json, "]", arrayStart);
   if(arrayEnd < 0) return;
   string tradesArray = StringSubstr(json, arrayStart + 1, arrayEnd - arrayStart - 1);

   int tradeStart = 0;
   while(true) {
      int objectStart = StringFind(tradesArray, "{", tradeStart);
      if(objectStart < 0) break;
      int objectEnd = StringFind(tradesArray, "}", objectStart);
      if(objectEnd < 0) break;

      string tradeObj = StringSubstr(tradesArray, objectStart, objectEnd - objectStart + 1);

      // Parse fields
      int    id = 0;
      string symbol = "";
      string action = "";
      double sl = 0, tp = 0, price = 0, riskValue = 0;

      if(!ParseTradeJson(tradeObj, id, symbol, action, sl, tp, price, riskValue)) {
         if(EnableLogging) Print("[PARSE] Failed to parse: ", tradeObj);
         tradeStart = objectEnd + 1;
         continue;
      }

      if(EnableLogging) Print("[TRADE] #", id, " ", action, " ", symbol, " SL=", sl, " TP=", tp, " Size=", riskValue);

      // Validate symbol
      string tradingSymbol = symbol;
      if(!SymbolSelect(tradingSymbol, true)) {
         if(EnableLogging) Print("[TRADE] Symbol not found: ", tradingSymbol);
         ConfirmTrade(id, "failed", 0, 0, "Symbol not found: " + tradingSymbol);
         tradeStart = objectEnd + 1;
         continue;
      }

      // ── Determine lot size ──
      // Use the exact size from TradingView (via risk_value with risk_type="lots")
      // Fall back to FallbackLots if TV sends 0
      double volume = riskValue > 0 ? riskValue : FallbackLots;
      volume = NormalizeLots(tradingSymbol, volume);

      if(EnableLogging) Print("[LOTS] ", volume, " (TV size=", riskValue, " fallback=", FallbackLots, ")");

      ulong ticketId = 0;
      bool executed = false;

      // ── CLOSE_PARTIAL ──
      if(action == "CLOSE_PARTIAL") {
         double pctToClose = riskValue > 0 ? riskValue : 50;
         if(pctToClose > 100) pctToClose = 100;

         bool hasPos = false;
         for(int p = PositionsTotal() - 1; p >= 0; p--) {
            ulong t = PositionGetTicket(p);
            if(PositionSelectByTicket(t) &&
               PositionGetString(POSITION_SYMBOL) == tradingSymbol &&
               (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
               hasPos = true; break;
            }
         }
         if(!hasPos) {
            ConfirmTrade(id, "executed", 0, 0);
            tradeStart = objectEnd + 1;
            continue;
         }

         datetime tBefore = TimeCurrent();
         executed = ClosePartialByMagic(tradingSymbol, pctToClose);
         if(executed) {
            double closeProfit = 0;
            Sleep(300);
            if(HistorySelect(tBefore - 5, TimeCurrent() + 5)) {
               int dealsN = HistoryDealsTotal();
               for(int di = 0; di < dealsN; di++) {
                  ulong dTicket = HistoryDealGetTicket(di);
                  if(HistoryDealGetString(dTicket, DEAL_SYMBOL) == tradingSymbol &&
                     (ulong)HistoryDealGetInteger(dTicket, DEAL_MAGIC) == (ulong)MagicNumber &&
                     HistoryDealGetInteger(dTicket, DEAL_ENTRY) == DEAL_ENTRY_OUT &&
                     HistoryDealGetInteger(dTicket, DEAL_TIME) >= tBefore) {
                     closeProfit += HistoryDealGetDouble(dTicket, DEAL_PROFIT)
                                  + HistoryDealGetDouble(dTicket, DEAL_SWAP)
                                  + HistoryDealGetDouble(dTicket, DEAL_COMMISSION);
                  }
               }
            }
            ConfirmTrade(id, "executed", closeProfit, 0);
            dailyTradeCount++;
         } else {
            ConfirmTrade(id, "failed", 0, 0, "Partial close failed for " + tradingSymbol);
         }
         tradeStart = objectEnd + 1;
         continue;
      }

      // ── CLOSE (full) ──
      if(StringFind(action, "CLOSE") >= 0) {
         bool hasPos = false;
         for(int p = PositionsTotal() - 1; p >= 0; p--) {
            ulong t = PositionGetTicket(p);
            if(PositionSelectByTicket(t) &&
               PositionGetString(POSITION_SYMBOL) == tradingSymbol &&
               (ulong)PositionGetInteger(POSITION_MAGIC) == (ulong)MagicNumber) {
               hasPos = true; break;
            }
         }
         if(!hasPos) {
            if(EnableLogging) Print("[CLOSE] No open positions on ", tradingSymbol, " — already closed");
            ConfirmTrade(id, "executed", 0, 0);
            tradeStart = objectEnd + 1;
            continue;
         }

         datetime tBefore = TimeCurrent();
         executed = CloseAllByMagic(tradingSymbol);
         if(executed) {
            double closeProfit = 0;
            Sleep(300);
            if(HistorySelect(tBefore - 5, TimeCurrent() + 5)) {
               int dealsN = HistoryDealsTotal();
               for(int di = 0; di < dealsN; di++) {
                  ulong dTicket = HistoryDealGetTicket(di);
                  if(HistoryDealGetString(dTicket, DEAL_SYMBOL) == tradingSymbol &&
                     (ulong)HistoryDealGetInteger(dTicket, DEAL_MAGIC) == (ulong)MagicNumber &&
                     HistoryDealGetInteger(dTicket, DEAL_ENTRY) == DEAL_ENTRY_OUT &&
                     HistoryDealGetInteger(dTicket, DEAL_TIME) >= tBefore) {
                     closeProfit += HistoryDealGetDouble(dTicket, DEAL_PROFIT)
                                  + HistoryDealGetDouble(dTicket, DEAL_SWAP)
                                  + HistoryDealGetDouble(dTicket, DEAL_COMMISSION);
                  }
               }
            }
            if(EnableLogging) Print("[CLOSE] Closed ", tradingSymbol, " P&L=", closeProfit);
            ConfirmTrade(id, "executed", closeProfit, 0);
            dailyTradeCount++;
         } else {
            ConfirmTrade(id, "failed", 0, 0, "Close failed for " + tradingSymbol);
         }
         tradeStart = objectEnd + 1;
         continue;
      }

      // ── BUY ──
      if(StringFind(action, "BUY") >= 0 || (StringFind(action, "ENTRY") >= 0 && StringFind(action, "LONG") >= 0)) {
         // Daily trade cap (skip entry but allow closes through)
         if(dailyTradeCount >= MaxDailyTrades) {
            if(EnableLogging) Print("[CAP] Daily trade limit reached (", MaxDailyTrades, ") — skipping BUY");
            ConfirmTrade(id, "failed", 0, 0, "Daily trade limit reached");
            tradeStart = objectEnd + 1;
            continue;
         }
         if(HasExistingPosition(tradingSymbol, true)) {
            if(EnableLogging) Print("[DUP] BUY #", id, " skipped — position already exists on ", tradingSymbol);
            ConfirmTrade(id, "executed", 0, 0);
            tradeStart = objectEnd + 1;
            continue;
         }
         if(DetectAndCloseHedge(tradingSymbol, true)) {
            ConfirmTrade(id, "failed", 0, 0, "[HEDGE] Opposing SELL closed — trade not opened");
            tradeStart = objectEnd + 1;
            continue;
         }
         executed = ExecuteTradeWithRetry(tradingSymbol, true, volume, sl, tp, ticketId);
      }
      // ── SELL ──
      else if(StringFind(action, "SELL") >= 0 || (StringFind(action, "ENTRY") >= 0 && StringFind(action, "SHORT") >= 0)) {
         if(dailyTradeCount >= MaxDailyTrades) {
            if(EnableLogging) Print("[CAP] Daily trade limit reached (", MaxDailyTrades, ") — skipping SELL");
            ConfirmTrade(id, "failed", 0, 0, "Daily trade limit reached");
            tradeStart = objectEnd + 1;
            continue;
         }
         if(HasExistingPosition(tradingSymbol, false)) {
            if(EnableLogging) Print("[DUP] SELL #", id, " skipped — position already exists on ", tradingSymbol);
            ConfirmTrade(id, "executed", 0, 0);
            tradeStart = objectEnd + 1;
            continue;
         }
         if(DetectAndCloseHedge(tradingSymbol, false)) {
            ConfirmTrade(id, "failed", 0, 0, "[HEDGE] Opposing BUY closed — trade not opened");
            tradeStart = objectEnd + 1;
            continue;
         }
         executed = ExecuteTradeWithRetry(tradingSymbol, false, volume, sl, tp, ticketId);
      }
      else {
         if(EnableLogging) Print("[TRADE] Unknown action '", action, "' — skipping");
         ConfirmTrade(id, "failed", 0, 0, "Unknown action: " + action);
         tradeStart = objectEnd + 1;
         continue;
      }

      // Confirm BUY/SELL result
      if(executed) {
         ConfirmTrade(id, "executed", 0, ticketId);
         TrackPosition(id, ticketId);
         dailyTradeCount++;
      } else {
         ConfirmTrade(id, "failed", 0, 0,
            "MT5 Error " + IntegerToString(trade.ResultRetcode()) + ": " + trade.ResultRetcodeDescription());
      }

      tradeStart = objectEnd + 1;
   }
}
//+------------------------------------------------------------------+
