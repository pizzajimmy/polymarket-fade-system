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
    .addItem("Evaluate Selected Market", "openEvaluationDialog")
    .addItem("Setup Sheets", "setupSheets")
    .addSeparator()
    .addItem("Prune Low-Quality Alerts", "pruneRawAlerts")
    .addSeparator()
    .addItem("Calibration Report", "runCalibrationReport")
    .addToUi();
}


// ── Sheet setup ───────────────────────────────────────────────────────────────

function setupSheets() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // Raw Alerts — ensure header includes cols M and N
  var rawSheet = ss.getSheetByName("Raw Alerts") || ss.insertSheet("Raw Alerts");
  if (rawSheet.getLastColumn() < 14 || rawSheet.getRange("A1").getValue() === "") {
    rawSheet.getRange("A1:N1").setValues([[
      "Timestamp", "Market Name", "URL", "Category", "Direction",
      "Price Before", "Price After", "Change (pts)", "Volume 24h",
      "Vol Multiple", "Liquidity", "Ambient Vol",
      "Quality Score", "Quality Flags"
    ]]);
    rawSheet.getRange("A1:N1").setFontWeight("bold");
  }

  // Evaluations Log
  var evalSheet = ss.getSheetByName("Evaluations Log") || ss.insertSheet("Evaluations Log");
  if (evalSheet.getRange("A1").getValue() === "") {
    evalSheet.getRange("A1:Y1").setValues([[
      "Eval Timestamp", "Alert Timestamp", "Market Name", "Market URL",
      "Category", "Direction", "Alert Price", "Price at Evaluation",
      "Edge at Evaluation", "Fair Value Estimate", "Classification",
      "Sub-classification", "News Source", "Time to Evaluate (hrs)",
      "Verdict", "Thesis", "Quality Score", "Entered Trade?",
      "Entry Price", "Exit Price", "Exit Reason", "Days Held",
      "P&L (pts)", "Classification Correct?", "Notes"
    ]]);
    evalSheet.getRange("A1:Y1").setFontWeight("bold");

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
  }

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

  var data = sheet.getRange(row, 1, 1, 14).getValues()[0];
  var alertData = {
    alertTimestamp: data[0] ? data[0].toString() : "",
    marketName:     data[1] || "",
    url:            data[2] || "",
    category:       data[3] || "",
    direction:      data[4] || "",
    priceBefore:    data[5] || "",
    priceAfter:     data[6] || "",
    changePts:      data[7] || "",
    volume24h:      data[8] || "",
    volMultiple:    data[9] || "",
    liquidity:      data[10] || "",
    ambientVol:     data[11] || "",
    qualityScore:   data[12] || 0,
    qualityFlags:   data[13] || "",
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
