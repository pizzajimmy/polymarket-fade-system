/**
 * Code.gs — Polymarket Fade System — Google Sheets Apps Script
 *
 * Paste into Extensions → Apps Script in your tracking spreadsheet.
 *
 * Tabs managed:
 *   Raw Alerts      — auto-populated by scanner.py (cols A-N)
 *   Evaluations Log — human evaluation entries (cols A-Y)
 *   Positions Log   — GO trades only
 *
 * Setup:
 *   1. Extensions → Apps Script → Project Settings → Script Properties
 *      Add: CLAUDE_API_KEY = sk-ant-...
 *   2. Run setupSheets() once to create/format tabs
 */

// ── Menu ──────────────────────────────────────────────────────────────────────

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Polymarket")
    .addItem("Evaluate Selected Market",       "openEvaluationDialog")
    .addItem("Setup / Fix Sheets",             "setupSheets")
    .addSeparator()
    .addItem("Refresh Prices & Resolution",    "refreshEvaluations")
    .addItem("Fix Market URLs",                "fixMarketUrls")
    .addItem("Prune Low-Quality Alerts",       "pruneRawAlerts")
    .addSeparator()
    .addItem("Calibration Report",             "runCalibrationReport")
    .addToUi();
}


// ── Sheet setup ───────────────────────────────────────────────────────────────

function setupSheets() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // Raw Alerts — ensure header includes cols M and N
  var rawSheet = ss.getSheetByName("Raw Alerts") || ss.insertSheet("Raw Alerts");
  if (rawSheet.getLastColumn() < 18 || rawSheet.getRange("A1").getValue() === "") {
    rawSheet.getRange("A1:R1").setValues([[
      "Timestamp", "Market Name", "URL", "Category", "Direction",
      "Price Before", "Price After", "Change (pts)", "Volume 24h",
      "Vol Multiple", "Liquidity", "Ambient Vol",
      "Quality Score", "Quality Flags", "Pre-Alert Price",
      "Best Bid", "Best Ask", "Spread (pts)"
    ]]);
    rawSheet.getRange("A1:R1").setFontWeight("bold");
  }

  // Evaluations Log — always rewrite headers so stale layouts get fixed.
  var evalSheet = ss.getSheetByName("Evaluations Log") || ss.insertSheet("Evaluations Log");
  evalSheet.getRange("A1:AB1").setValues([[
    // ── Alert context (A–N) ──
    "Eval Timestamp",          // A
    "Alert Timestamp",         // B
    "Market Name",             // C
    "Market URL",              // D
    "Category",                // E
    "Direction",               // F
    "Alert Price",             // G
    "Price at Evaluation",     // H
    "Edge at Evaluation",      // I
    "Fair Value Estimate",     // J
    "Classification",          // K
    "Sub-classification",      // L
    "News Source",             // M
    "Time to Evaluate (hrs)",  // N
    // ── Decision (O–Q) ──
    "Verdict",                 // O
    "Thesis",                  // P
    "Quality Score",           // Q
    // ── Trade outcome (R–Y) — filled manually ──
    "Entered Trade?",          // R
    "Entry Price",             // S
    "Exit Price",              // T
    "Exit Reason",             // U
    "Days Held",               // V
    "P&L (pts)",               // W
    "Classification Correct?", // X
    "Notes",                   // Y
    // ── Live market data (Z–AB) — filled by Refresh ──
    "Current Price",           // Z
    "Resolution",              // AA
    "Last Checked",            // AB
  ]]);
  evalSheet.getRange("A1:AB1").setFontWeight("bold");

  // Data validation — only apply once (idempotent on already-validated ranges)
  var classRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["SENTIMENT", "STRUCTURAL", "UNCLEAR"], true).build();
  evalSheet.getRange("K2:K1000").setDataValidation(classRule);

  var subclassRule = SpreadsheetApp.newDataValidation()
    .requireValueInList([
      "PROCEDURAL_DELAY", "NEGATIVE_COMMENTARY", "UNFAVOURABLE_POLL",
      "PERSONAL_CONDUCT", "LEAKED_DOCUMENT",
      "HARD_OUTCOME", "REGULATORY_RULING", "FACTUAL_REVELATION",
      "TIMELINE_COMPRESSION", "UNCLEAR"
    ], true).build();
  evalSheet.getRange("L2:L1000").setDataValidation(subclassRule);

  var sourceRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["POLYMARKET_COMMENTS", "MAINSTREAM_NEWS", "TWITTER", "OTHER"], true).build();
  evalSheet.getRange("M2:M1000").setDataValidation(sourceRule);

  var verdictRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["GO", "WEAK_GO", "PASS"], true).build();
  evalSheet.getRange("O2:O1000").setDataValidation(verdictRule);

  var tradedRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["YES", "NO"], true).build();
  evalSheet.getRange("R2:R1000").setDataValidation(tradedRule);

  var exitRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["TARGET", "STOP", "STALE", "STRUCTURAL_UPDATE", "MANUAL"], true).build();
  evalSheet.getRange("U2:U1000").setDataValidation(exitRule);

  var correctRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["YES", "NO", "PARTIAL"], true).build();
  evalSheet.getRange("X2:X1000").setDataValidation(correctRule);

  var resolutionRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["PENDING", "YES", "NO", "AMBIGUOUS"], true).build();
  evalSheet.getRange("AA2:AA1000").setDataValidation(resolutionRule);

  // Positions Log
  var posSheet = ss.getSheetByName("Positions Log") || ss.insertSheet("Positions Log");
  if (posSheet.getRange("A1").getValue() === "") {
    posSheet.getRange("A1:J1").setValues([[
      "Entry Date", "Market Name", "URL", "Direction",
      "Entry Price", "Size", "Thesis",
      "Exit Price", "Exit Date", "P&L (pts)"
    ]]);
    posSheet.getRange("A1:J1").setFontWeight("bold");
  }

  SpreadsheetApp.getUi().alert("Sheets setup complete.");
}


// ── Evaluation dialog ─────────────────────────────────────────────────────────

function openEvaluationDialog() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getActiveSheet();

  if (sheet.getName() !== "Raw Alerts") {
    SpreadsheetApp.getUi().alert("Please select a row in the Raw Alerts sheet first.");
    return;
  }

  var row = sheet.getActiveRange().getRow();
  if (row <= 1) {
    SpreadsheetApp.getUi().alert("Please select a data row (not the header).");
    return;
  }

  var data = sheet.getRange(row, 1, 1, 18).getValues()[0];
  var alertData = {
    alertTimestamp:  data[0] ? data[0].toString() : "",
    marketName:      data[1] || "",
    url:             data[2] || "",
    category:        data[3] || "",
    direction:       data[4] || "",
    priceBefore:     data[5] || "",
    priceAfter:      data[6] || "",
    changePts:       data[7] || "",
    volume24h:       data[8] || "",
    volMultiple:     data[9] || "",
    liquidity:       data[10] || "",
    ambientVol:      data[11] || "",
    qualityScore:    data[12] || 0,
    qualityFlags:    data[13] || "",
    preAlertPrice:   data[14] || "",
    bestBid:         data[15] || "",
    bestAsk:         data[16] || "",
    spreadPts:       data[17] || "",
  };

  var html = HtmlService.createTemplateFromFile("EvaluationDialog");
  html.alertData = JSON.stringify(alertData);
  var dlg = html.evaluate().setWidth(640).setHeight(920).setTitle("Evaluate Market");
  SpreadsheetApp.getUi().showModalDialog(dlg, "Evaluate Market");
}


// ── Claude API analysis ───────────────────────────────────────────────────────

function analyzeWithClaude(marketData) {
  var apiKey = PropertiesService.getScriptProperties().getProperty("CLAUDE_API_KEY");
  if (!apiKey) {
    return { error: "CLAUDE_API_KEY not set. Go to Extensions → Apps Script → Project Settings → Script Properties." };
  }

  var direction   = marketData.direction  || "DROP";
  var priceBefore = parseFloat(marketData.priceBefore) || 0;
  var priceAfter  = parseFloat(marketData.priceAfter)  || 0;
  var changePts   = Math.abs(priceBefore - priceAfter).toFixed(1);
  var newsContext = (marketData.newsContext || "").trim();

  var subclassOptions = direction === "DROP" || !direction
    ? "PROCEDURAL_DELAY, NEGATIVE_COMMENTARY, UNFAVOURABLE_POLL, PERSONAL_CONDUCT, LEAKED_DOCUMENT, HARD_OUTCOME, REGULATORY_RULING, FACTUAL_REVELATION, TIMELINE_COMPRESSION, UNCLEAR"
    : "HARD_OUTCOME, REGULATORY_RULING, FACTUAL_REVELATION, TIMELINE_COMPRESSION, PROCEDURAL_DELAY, NEGATIVE_COMMENTARY, UNFAVOURABLE_POLL, PERSONAL_CONDUCT, LEAKED_DOCUMENT, UNCLEAR";

  var prompt =
    "You are an expert prediction market analyst specialising in mean-reversion / fade trading.\n" +
    "Your job is to evaluate whether a price move on Polymarket represents a temporary sentiment\n" +
    "overcorrection (SENTIMENT) or a genuine structural update to the market's probability (STRUCTURAL).\n\n" +
    "MARKET: " + (marketData.question || marketData.marketName || "") + "\n" +
    "CATEGORY: " + (marketData.category || "unknown") + "\n" +
    "ALERT TYPE: " + direction + "\n" +
    "PRICE MOVE: " + priceBefore + "¢ → " + priceAfter + "¢  (−" + changePts + " pts)\n" +
    "CURRENT PRICE: " + priceAfter + "¢\n" +
    "VOLUME MULTIPLE: " + (marketData.volMultiple || "unknown") + "× normal\n" +
    "QUALITY SCORE: " + (marketData.qualityScore || 0) + "/100  [flags: " + (marketData.qualityFlags || "none") + "]\n\n" +
    "NEWS / CONTEXT PROVIDED BY ANALYST:\n" +
    (newsContext || "(none — base your analysis on the market characteristics alone)") + "\n\n" +
    "─────────────────────────────────────────────\n" +
    "Respond with ONLY a raw JSON object (no markdown, no code fences). Schema:\n" +
    "{\n" +
    '  "classification": "SENTIMENT" | "STRUCTURAL" | "UNCLEAR",\n' +
    '  "subClassification": "<one of: ' + subclassOptions + '>",\n' +
    '  "confidence": "HIGH" | "MEDIUM" | "LOW",\n' +
    '  "fairValue": <integer 0-100, your estimate of the true probability in cents>,\n' +
    '  "fairValueReasoning": "<2-3 sentences: base rate + how the news shifts it>",\n' +
    '  "thesis": "<one crisp sentence: why the move is an overcorrection and what the recovery catalyst is>",\n' +
    '  "keyRisk": "<one sentence: the specific condition that would mean the fade thesis is wrong>",\n' +
    '  "newsSourceGuess": "POLYMARKET_COMMENTS" | "MAINSTREAM_NEWS" | "TWITTER" | "OTHER"\n' +
    "}";

  try {
    var response = UrlFetchApp.fetch("https://api.anthropic.com/v1/messages", {
      method: "post",
      muteHttpExceptions: true,
      headers: {
        "x-api-key":         apiKey,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
      },
      payload: JSON.stringify({
        model:      "claude-opus-4-6",
        max_tokens: 600,
        messages:   [{ role: "user", content: prompt }],
      }),
    });

    var code = response.getResponseCode();
    if (code !== 200) {
      var errBody = response.getContentText();
      try { errBody = JSON.parse(errBody).error.message; } catch(e) {}
      return { error: "Claude API returned HTTP " + code + ": " + errBody };
    }

    var body  = JSON.parse(response.getContentText());
    var text  = body.content[0].text.trim();

    // Strip markdown code fences if Claude wraps them anyway
    text = text.replace(/^```(?:json)?\s*/i, "").replace(/\s*```\s*$/i, "").trim();

    var result = JSON.parse(text);
    return result;

  } catch (e) {
    return { error: e.message };
  }
}


// ── Gamma API price fetch ─────────────────────────────────────────────────────

function fetchMarketPrice(slugOrEventSlug, marketQuestion) {
  function parsePrices(m) {
    try {
      var prices = m.outcomePrices;
      if (typeof prices === "string") prices = JSON.parse(prices);
      return prices && prices.length > 0 ? parseFloat(prices[0]) * 100 : null;
    } catch (e) { return null; }
  }

  function fetchBySlug(slug) {
    var url = "https://gamma-api.polymarket.com/markets?slug=" + encodeURIComponent(slug) + "&limit=1";
    var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (resp.getResponseCode() !== 200) return null;
    var data = JSON.parse(resp.getContentText());
    return (data && data.length > 0) ? data[0] : null;
  }

  function fetchByEventSlug(eventSlug, question) {
    var url = "https://gamma-api.polymarket.com/markets?eventSlug=" + encodeURIComponent(eventSlug) + "&limit=50";
    var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (resp.getResponseCode() !== 200) return null;
    var data = JSON.parse(resp.getContentText());
    if (!data || data.length === 0) return null;
    if (question) {
      var qLower = question.toLowerCase();
      for (var i = 0; i < data.length; i++) {
        if ((data[i].question || "").toLowerCase() === qLower) return data[i];
      }
    }
    data.sort(function(a, b) { return (b.liquidity || 0) - (a.liquidity || 0); });
    return data[0];
  }

  try {
    var market = fetchBySlug(slugOrEventSlug);
    if (market) {
      var price = parsePrices(market);
      if (price !== null) return { price: price };
    }

    market = fetchByEventSlug(slugOrEventSlug, marketQuestion);
    if (market) {
      var price = parsePrices(market);
      if (price !== null) return { price: price };
    }

    return { error: "No price found for: " + slugOrEventSlug };
  } catch (e) {
    return { error: e.message };
  }
}


// ── Prune Raw Alerts ──────────────────────────────────────────────────────────

function pruneRawAlerts() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Raw Alerts");
  if (!sheet) { SpreadsheetApp.getUi().alert("No Raw Alerts sheet found."); return; }

  var ui = SpreadsheetApp.getUi();
  var resp = ui.alert(
    "Prune Low-Quality Alerts",
    "This will delete rows that fail any screening criteria:\n\n" +
    "• Category = Sports\n" +
    "• Category = Crypto (not tradeable with fade strategy)\n" +
    "• |Change| < 15 pts\n" +
    "• Vol Multiple < 1.5×\n" +
    "• Liquidity < $1,000\n" +
    "• Quality Score < 25 (if scored)\n\n" +
    "Continue?",
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) { ui.alert("No data rows to prune."); return; }

  // Col indices (0-based)
  var COL_CATEGORY = 3;   // D — Category
  var COL_CHANGE   = 7;   // H — Change (pts)
  var COL_VOLMULT  = 9;   // J — Vol Multiple
  var COL_LIQ      = 10;  // K — Liquidity
  var COL_SCORE    = 12;  // M — Quality Score

  // Categories that don't suit the fade strategy
  var EXCLUDED_CATEGORIES = { "sports": true, "crypto": true };

  var rowsToDelete = [];
  for (var i = 1; i < data.length; i++) {
    var row      = data[i];
    var category = (row[COL_CATEGORY] || "").toString().toLowerCase().trim();
    var change   = Math.abs(parseFloat(row[COL_CHANGE])  || 0);
    var volMult  = parseFloat(row[COL_VOLMULT]) || 0;
    var liq      = parseFloat(row[COL_LIQ])     || 0;
    var scoreRaw = row[COL_SCORE];
    var score    = (scoreRaw !== "" && scoreRaw !== null) ? parseInt(scoreRaw) : null;

    var fail = EXCLUDED_CATEGORIES[category] ||
               (change < 15) || (volMult < 1.5) || (liq < 1000) ||
               (score !== null && score < 25);
    if (fail) rowsToDelete.push(i + 1);
  }

  if (rowsToDelete.length === 0) {
    ui.alert("No rows to prune — all " + (data.length - 1) + " rows pass the filters.");
    return;
  }

  rowsToDelete.reverse();
  rowsToDelete.forEach(function(rowNum) { sheet.deleteRow(rowNum); });

  ui.alert("Pruned " + rowsToDelete.length + " rows. " +
           (data.length - 1 - rowsToDelete.length) + " rows remain.");
}


// ── Write evaluation row ──────────────────────────────────────────────────────

function logEvaluation(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var evalSheet = ss.getSheetByName("Evaluations Log");
  if (!evalSheet) {
    setupSheets();
    evalSheet = ss.getSheetByName("Evaluations Log");
  }

  var now = new Date();
  var timeToEval = data.alertTimestamp
    ? ((now - new Date(data.alertTimestamp)) / 3600000).toFixed(2)
    : "";

  var row = [
    now,
    data.alertTimestamp    || "",
    data.marketName        || "",
    data.url               || "",
    data.category          || "",
    data.direction         || "",
    data.alertPrice        || "",
    data.priceAtEval       || "",
    data.edgeAtEval        || "",
    data.fairValue         || "",
    data.classification    || "",
    data.subClassification || "",
    data.newsSource        || "",
    timeToEval,
    data.verdict           || "",
    data.thesis            || "",
    data.qualityScore      || "",
    "", "", "", "", "", "", "", "",  // R–Y filled later
  ];

  evalSheet.appendRow(row);

  if (data.verdict === "GO" || data.verdict === "WEAK_GO") {
    var ui = SpreadsheetApp.getUi();
    var resp = ui.alert(
      "Log to Positions?",
      "Verdict is " + data.verdict + ". Add a row to Positions Log?",
      ui.ButtonSet.YES_NO
    );
    if (resp === ui.Button.YES) logPosition(data);
  }

  return "ok";
}


function logPosition(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var posSheet = ss.getSheetByName("Positions Log");
  if (!posSheet) return;
  posSheet.appendRow([
    new Date(),
    data.marketName  || "",
    data.url         || "",
    data.direction   || "",
    data.priceAtEval || "",
    "",
    data.thesis      || "",
    "", "", "",
  ]);
}


// ── Market status (price + resolution) ───────────────────────────────────────

function checkMarketStatus(slugOrEventSlug, question) {
  /**
   * Fetches current YES price, resolution status, and correct Polymarket URL.
   * Returns { price, resolution, closed, correctUrl }
   *   resolution: "PENDING" | "YES" | "NO" | "AMBIGUOUS" | "UNKNOWN"
   *   correctUrl: eventSlug-based URL if available, otherwise slug-based
   */
  function parsePrices(m) {
    try {
      var prices = m.outcomePrices;
      if (typeof prices === "string") prices = JSON.parse(prices);
      return (prices && prices.length > 0) ? parseFloat(prices[0]) * 100 : null;
    } catch (e) { return null; }
  }

  function correctUrlFromMarket(m) {
    var eSlug = m.eventSlug || m.groupSlug || "";
    var slug  = m.slug || "";
    var best  = eSlug || slug;
    return best ? "https://polymarket.com/event/" + best : "";
  }

  function fetchRaw(slug, asEventSlug) {
    var param = asEventSlug ? "eventSlug" : "slug";
    // No closed= filter — we need both active and resolved markets
    var url = "https://gamma-api.polymarket.com/markets?" + param + "=" +
              encodeURIComponent(slug) + "&limit=50";
    try {
      var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
      if (resp.getResponseCode() !== 200) return null;
      var data = JSON.parse(resp.getContentText());
      if (!data || data.length === 0) return null;
      if (question) {
        var q = question.toLowerCase();
        for (var i = 0; i < data.length; i++) {
          if ((data[i].question || "").toLowerCase() === q) return data[i];
        }
      }
      data.sort(function(a, b) { return (b.liquidity || 0) - (a.liquidity || 0); });
      return data[0];
    } catch (e) { return null; }
  }

  try {
    var market = fetchRaw(slugOrEventSlug, false) ||
                 fetchRaw(slugOrEventSlug, true);

    if (!market) return { price: null, resolution: "UNKNOWN", closed: false, correctUrl: "" };

    var price      = parsePrices(market);
    var closed     = market.closed === true || market.active === false;
    var correctUrl = correctUrlFromMarket(market);

    var resolution = "PENDING";
    if (closed) {
      if (price === null)   resolution = "AMBIGUOUS";
      else if (price >= 99) resolution = "YES";
      else if (price <= 1)  resolution = "NO";
      else                  resolution = "AMBIGUOUS";
    }

    return { price: price, resolution: resolution, closed: closed, correctUrl: correctUrl };
  } catch (e) {
    return { price: null, resolution: "UNKNOWN", closed: false, correctUrl: "" };
  }
}


// ── Refresh prices and resolution in Evaluations Log ─────────────────────────

function refreshEvaluations() {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Evaluations Log");
  if (!sheet) {
    SpreadsheetApp.getUi().alert("No Evaluations Log sheet found. Run Setup / Fix Sheets first.");
    return;
  }

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) {
    SpreadsheetApp.getUi().alert("No evaluation rows found.");
    return;
  }

  // Col indices (0-based)
  var COL_URL            = 3;   // D — Market URL
  var COL_QUESTION       = 2;   // C — Market Name
  var COL_DIRECTION      = 5;   // F — Direction
  var COL_CLASSIFICATION = 10;  // K — Classification
  var COL_CLASS_CORRECT  = 23;  // X — Classification Correct?
  var COL_PRICE_NOW      = 25;  // Z — Current Price
  var COL_RESOLUTION     = 26;  // AA — Resolution
  var COL_CHECKED        = 27;  // AB — Last Checked

  var now        = new Date();
  var updated    = 0;
  var resolved   = 0;
  var autoScored = 0;
  var urlsFixed  = 0;
  var errors     = 0;

  for (var i = 1; i < data.length; i++) {
    var url      = (data[i][COL_URL] || "").toString().trim();
    var question = (data[i][COL_QUESTION] || "").toString().trim();
    if (!url) continue;

    var match = url.match(/polymarket\.com\/event\/([^/?#]+)/);
    if (!match) continue;
    var slug = match[1];

    try {
      var status = checkMarketStatus(slug, question);
      var rowNum = i + 1;

      if (status.price !== null) {
        sheet.getRange(rowNum, COL_PRICE_NOW + 1).setValue(parseFloat(status.price.toFixed(1)));
      }
      sheet.getRange(rowNum, COL_RESOLUTION + 1).setValue(status.resolution);
      sheet.getRange(rowNum, COL_CHECKED    + 1).setValue(now);

      // Fix URL if the API gave us a better one (eventSlug-based)
      if (status.correctUrl && status.correctUrl !== url) {
        sheet.getRange(rowNum, COL_URL + 1).setValue(status.correctUrl);
        urlsFixed++;
      }

      updated++;
      if (status.resolution === "YES" || status.resolution === "NO") {
        resolved++;

        // ── Feature 4: Auto classification accuracy ──────────────
        var classification = (data[i][COL_CLASSIFICATION] || "").toString().trim();
        var classCorrect   = (data[i][COL_CLASS_CORRECT] || "").toString().trim();
        var direction      = (data[i][COL_DIRECTION] || "").toString().trim();

        if (classification && !classCorrect) {
          // SENTIMENT = overcorrection, expects recovery (mean-revert)
          // STRUCTURAL = genuine repricing, expects price to stick
          // For DROP: recovery = resolved YES (price went back up)
          // For SPIKE: recovery = resolved NO (price came back down)
          var recovered = (direction === "DROP" && status.resolution === "YES") ||
                          (direction === "SPIKE" && status.resolution === "NO");

          var autoResult;
          if (classification === "UNCLEAR") {
            autoResult = "PARTIAL";
          } else if (classification === "SENTIMENT") {
            autoResult = recovered ? "YES" : "NO";
          } else if (classification === "STRUCTURAL") {
            autoResult = recovered ? "NO" : "YES";
          }

          if (autoResult) {
            sheet.getRange(rowNum, COL_CLASS_CORRECT + 1).setValue(autoResult);
            autoScored++;
          }
        }
      }

      Utilities.sleep(400);
    } catch (e) {
      errors++;
    }
  }

  SpreadsheetApp.getUi().alert(
    "Refresh complete.\n\n" +
    "• " + updated    + " rows updated\n" +
    "• " + resolved   + " markets resolved (YES/NO)\n" +
    (autoScored > 0 ? "• " + autoScored + " classification(s) auto-scored\n" : "") +
    (urlsFixed > 0  ? "• " + urlsFixed  + " URL(s) corrected\n" : "") +
    (errors > 0     ? "• " + errors    + " errors (check logs)\n" : "")
  );
}


// ── Comparable alert lookup (Feature 3) ──────────────────────────────────────

function findComparableAlerts(category, classification, changePts, direction) {
  /**
   * Search Evaluations Log for past alerts with similar characteristics.
   * Returns array of objects for display in the evaluation dialog.
   */
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Evaluations Log");
  if (!sheet) return [];

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  var COL = {
    MARKET_NAME: 2, CATEGORY: 4, DIRECTION: 5, ALERT_PRICE: 6,
    PRICE_AT_EVAL: 7, EDGE: 8, FAIR_VALUE: 9, CLASSIFICATION: 10,
    VERDICT: 14, PNL: 22, CLASS_CORRECT: 23, CURRENT_PRICE: 25,
    RESOLUTION: 26,
  };

  var targetMag = Math.abs(parseFloat(changePts) || 0);
  var results = [];

  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var rowCat   = (row[COL.CATEGORY] || "").toString().toLowerCase().trim();
    var rowDir   = (row[COL.DIRECTION] || "").toString().trim();
    var rowClass = (row[COL.CLASSIFICATION] || "").toString().trim();
    var rowEdge  = Math.abs(parseFloat(row[COL.EDGE]) || 0);

    // Must match category and direction
    if (rowCat !== (category || "").toLowerCase().trim()) continue;
    if (rowDir !== (direction || "").trim()) continue;

    // Move magnitude within 60%-150% of target
    if (targetMag > 0 && (rowEdge < targetMag * 0.6 || rowEdge > targetMag * 1.5)) continue;

    // Prefer same classification if provided, but include all matches
    var classMatch = !classification || rowClass === classification;

    results.push({
      marketName:   (row[COL.MARKET_NAME] || "").toString().substring(0, 60),
      alertPrice:   row[COL.ALERT_PRICE] || "",
      classification: rowClass,
      verdict:      (row[COL.VERDICT] || "").toString(),
      pnl:          row[COL.PNL] || "",
      classCorrect: (row[COL.CLASS_CORRECT] || "").toString(),
      resolution:   (row[COL.RESOLUTION] || "").toString(),
      currentPrice: row[COL.CURRENT_PRICE] || "",
      classMatch:   classMatch,
    });
  }

  // Sort: classification matches first, then most recent
  results.sort(function(a, b) {
    if (a.classMatch !== b.classMatch) return a.classMatch ? -1 : 1;
    return 0;
  });

  return results.slice(0, 8);
}


// ── Fix Market URLs (bulk repair) ────────────────────────────────────────────

function fixMarketUrls() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = SpreadsheetApp.getUi();

  var resp = ui.alert(
    "Fix Market URLs",
    "This will look up each market in the Gamma API and replace any incorrect\n" +
    "URLs with the correct eventSlug-based URL.\n\n" +
    "Sheets updated: Raw Alerts (col C), Evaluations Log (col D).\n\n" +
    "This may take a while for large sheets. Continue?",
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  var totalFixed = 0;

  // ── Raw Alerts — URL in col C (index 2), question in col B (index 1) ────────
  var rawSheet = ss.getSheetByName("Raw Alerts");
  if (rawSheet) {
    var rawData = rawSheet.getDataRange().getValues();
    for (var i = 1; i < rawData.length; i++) {
      var url      = (rawData[i][2] || "").toString().trim();
      var question = (rawData[i][1] || "").toString().trim();
      if (!url) continue;

      var match = url.match(/polymarket\.com\/event\/([^/?#]+)/);
      if (!match) continue;
      var slug = match[1];

      try {
        var status = checkMarketStatus(slug, question);
        if (status.correctUrl && status.correctUrl !== url) {
          rawSheet.getRange(i + 1, 3).setValue(status.correctUrl);
          totalFixed++;
        }
        Utilities.sleep(400);
      } catch (e) { /* skip on error */ }
    }
  }

  // ── Evaluations Log — URL in col D (index 3), question in col C (index 2) ──
  var evalSheet = ss.getSheetByName("Evaluations Log");
  if (evalSheet) {
    var evalData = evalSheet.getDataRange().getValues();
    for (var j = 1; j < evalData.length; j++) {
      var eUrl      = (evalData[j][3] || "").toString().trim();
      var eQuestion = (evalData[j][2] || "").toString().trim();
      if (!eUrl) continue;

      var eMatch = eUrl.match(/polymarket\.com\/event\/([^/?#]+)/);
      if (!eMatch) continue;
      var eSlug = eMatch[1];

      try {
        var eStatus = checkMarketStatus(eSlug, eQuestion);
        if (eStatus.correctUrl && eStatus.correctUrl !== eUrl) {
          evalSheet.getRange(j + 1, 4).setValue(eStatus.correctUrl);
          totalFixed++;
        }
        Utilities.sleep(400);
      } catch (e) { /* skip on error */ }
    }
  }

  ui.alert("Fix Market URLs complete.\n\n• " + totalFixed + " URL(s) updated.");
}


// ── Calibration report ────────────────────────────────────────────────────────

function runCalibrationReport() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var evalSheet = ss.getSheetByName("Evaluations Log");
  if (!evalSheet) { SpreadsheetApp.getUi().alert("No Evaluations Log sheet found."); return; }

  var data = evalSheet.getDataRange().getValues();
  if (data.length < 2) { SpreadsheetApp.getUi().alert("Not enough evaluation data yet."); return; }

  var COL = {
    evalTs: 0, alertTs: 1, marketName: 2, url: 3, category: 4,
    direction: 5, alertPrice: 6, priceAtEval: 7, edgeAtEval: 8,
    fairValue: 9, classification: 10, subClass: 11, newsSource: 12,
    timeToEval: 13, verdict: 14, thesis: 15, qualityScore: 16,
    enteredTrade: 17, entryPrice: 18, exitPrice: 19, exitReason: 20,
    daysHeld: 21, pnl: 22, classCorrect: 23, notes: 24,
  };

  var rows = data.slice(1);

  function groupStats(rows, keyFn) {
    var groups = {};
    rows.forEach(function(r) {
      var key = keyFn(r) || "unknown";
      if (!groups[key]) groups[key] = { total: 0, went: 0, correct: 0, pnlSum: 0, pnlCount: 0, timeSum: 0, timeCount: 0 };
      var g = groups[key];
      g.total++;
      if (r[COL.enteredTrade] === "YES") g.went++;
      if (r[COL.classCorrect] === "YES") g.correct++;
      var pnl = parseFloat(r[COL.pnl]);
      if (!isNaN(pnl)) { g.pnlSum += pnl; g.pnlCount++; }
      var tte = parseFloat(r[COL.timeToEval]);
      if (!isNaN(tte)) { g.timeSum += tte; g.timeCount++; }
    });
    return groups;
  }

  var byClass    = groupStats(rows, function(r) { return r[COL.classification]; });
  var bySubClass = groupStats(rows, function(r) { return r[COL.subClass]; });
  var byCategory = groupStats(rows, function(r) { return r[COL.category]; });

  var totalEdgeExp = 0, totalEdgeReal = 0, edgeCount = 0;
  rows.forEach(function(r) {
    var exp = parseFloat(r[COL.edgeAtEval]), real = parseFloat(r[COL.pnl]);
    if (!isNaN(exp) && !isNaN(real)) { totalEdgeExp += exp; totalEdgeReal += real; edgeCount++; }
  });

  var totalTime = 0, timeCount = 0;
  rows.forEach(function(r) {
    var t = parseFloat(r[COL.timeToEval]);
    if (!isNaN(t)) { totalTime += t; timeCount++; }
  });

  var totalCorrect = 0, correctCount = 0;
  rows.forEach(function(r) {
    if (r[COL.classCorrect] !== "") {
      correctCount++;
      if (r[COL.classCorrect] === "YES") totalCorrect++;
    }
  });

  var monthStr   = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM");
  var reportName = "Calibration — " + monthStr;
  var existing   = ss.getSheetByName(reportName);
  if (existing) ss.deleteSheet(existing);
  var report = ss.insertSheet(reportName);

  var output = [];
  output.push(["Polymarket Fade System — Calibration Report", monthStr]);
  output.push([]);
  output.push(["Total evaluations",         rows.length]);
  output.push(["Avg time to evaluate (hrs)", timeCount  ? (totalTime / timeCount).toFixed(1) : "n/a"]);
  output.push(["Classification accuracy",   correctCount ? ((totalCorrect / correctCount) * 100).toFixed(1) + "%" : "n/a"]);
  output.push(["Avg edge expected (pts)",   edgeCount   ? (totalEdgeExp  / edgeCount).toFixed(1) : "n/a"]);
  output.push(["Avg edge realised (pts)",   edgeCount   ? (totalEdgeReal / edgeCount).toFixed(1) : "n/a"]);
  output.push([]);

  output.push(["── By Classification ──"]);
  output.push(["Classification", "Total", "Went", "Correct", "Avg P&L"]);
  Object.keys(byClass).sort().forEach(function(k) {
    var g = byClass[k];
    output.push([k, g.total, g.went, g.correct + "/" + g.total,
      g.pnlCount ? (g.pnlSum / g.pnlCount).toFixed(1) : "n/a"]);
  });
  output.push([]);

  output.push(["── By Sub-classification ──"]);
  output.push(["Sub-class", "Total", "Went", "Avg P&L"]);
  Object.keys(bySubClass).sort().forEach(function(k) {
    var g = bySubClass[k];
    output.push([k, g.total, g.went,
      g.pnlCount ? (g.pnlSum / g.pnlCount).toFixed(1) : "n/a"]);
  });
  output.push([]);

  output.push(["── By Category ──"]);
  output.push(["Category", "Total", "Went", "Avg P&L"]);
  Object.keys(byCategory).sort().forEach(function(k) {
    var g = byCategory[k];
    output.push([k, g.total, g.went,
      g.pnlCount ? (g.pnlSum / g.pnlCount).toFixed(1) : "n/a"]);
  });

  report.getRange(1, 1, output.length, 5).setValues(
    output.map(function(r) { while (r.length < 5) r.push(""); return r; })
  );
  report.getRange("A1").setFontWeight("bold").setFontSize(13);
  report.autoResizeColumns(1, 5);
  ss.setActiveSheet(report);
  SpreadsheetApp.getUi().alert("Calibration report generated: " + reportName);
}
