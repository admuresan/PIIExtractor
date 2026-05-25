/**
 * Read genai/toolkits/architectural_instructions before making changes.
 * Presentation only; extraction runs on the Flask backend.
 */

(function mountEmailPiiLab() {
	function readBoot() {
		var el = document.getElementById("lpl-boot");
		if (!el || !el.textContent) return { examples: [], uiStrings: {} };
		try {
			return JSON.parse(el.textContent);
		} catch (e) {
			return { examples: [], uiStrings: {} };
		}
	}

	function typeCssClass(infoType) {
		var slug = String(infoType || "unknown").trim().replace(/\s+/g, "-");
		return "pii-type-" + slug.toLowerCase();
	}

	function format(template, map) {
		return String(template || "").replace(/\{(\w+)\}/g, function (_, k) {
			return map[k] != null ? String(map[k]) : "";
		});
	}

	/** Shallow extraction row clone for sane span correction. */
	function copyExtrRow(row) {
		return {
			start: row.start,
			end: row.end,
			info_type: row.info_type,
			info_value: row.info_value,
		};
	}

	/**
	 * When offsets disagree with info_value (model drift / Unicode quirks), relocate [start,end)
	 * to the best matching substring window around the advertised span before rendering.
	 */
	function reconcileRowSpansToBody(body, row) {
		var iv = row.info_value != null ? String(row.info_value) : "";
		var lim = body.length;
		var sRaw = Number(row.start);
		var eRaw = Number(row.end);
		var s = Math.max(0, Math.min(lim, Math.floor(sRaw)));
		var e = Math.max(0, Math.min(lim, Math.floor(eRaw)));

		function emit(ss, ee) {
			ss = Math.max(0, Math.min(lim, Math.floor(Number(ss))));
			ee = Math.max(0, Math.min(lim, Math.floor(Number(ee))));
			if (!(ee > ss && ee <= lim)) {
				return null;
			}
			var cx = copyExtrRow(row);
			cx.start = ss;
			cx.end = ee;
			return cx;
		}

		if (!(e > s)) {
			return null;
		}

		var slice = body.slice(s, e);
		if (!iv.trim()) {
			return emit(s, e);
		}

		function tryLocate(needle, origS, origE) {
			if (!needle.length || needle.length > lim) {
				return null;
			}
			var slack = Math.max(Math.floor(needle.length * 2), 380);
			var lo = Math.max(0, origS - slack);
			var boundary = Math.min(lim, origE + slack);
			var guard = 0;
			var bestIx = -1;
			var bestScore = Infinity;
			for (var ix = body.indexOf(needle, lo); ix !== -1; ix = body.indexOf(needle, ix + 1)) {
				guard += 1;
				if (guard > 280) {
					break;
				}
				var endIx = ix + needle.length;
				if (endIx > lim) {
					break;
				}
				if (ix > boundary) {
					break;
				}
				var overlapsOrig = ix < origE && endIx > origS;
				var penalty = overlapsOrig ? 0 : 8000 + Math.abs(ix - origS) * 2;
				var score = penalty + Math.abs(ix - origS) + Math.abs(endIx - origE);
				if (score < bestScore) {
					bestScore = score;
					bestIx = ix;
				}
			}
			if (bestIx < 0) {
				return null;
			}
			var endIx2 = bestIx + needle.length;
			var out = emit(bestIx, endIx2);
			return out;
		}

		if (slice === iv) {
			return emit(s, e);
		}

		var fromIv = tryLocate(iv, s, e);
		if (fromIv) {
			return fromIv;
		}
		var trimmed = iv.trim();
		if (trimmed !== iv) {
			var fromTrim = tryLocate(trimmed, s, e);
			if (fromTrim) {
				return fromTrim;
			}
		}

		return emit(s, e);
	}

	/** Sorted, clipped spans preserving overlaps — every row contributes breakpoints for markup. */
	function clipExtractionsForBody(body, extractions) {
		var rows = [];
		if (!body || typeof body !== "string") {
			return rows;
		}
		var arr = Array.isArray(extractions) ? extractions : [];
		for (var i = 0; i < arr.length; i++) {
			var r = reconcileRowSpansToBody(body, arr[i]);
			if (!r) {
				continue;
			}
			rows.push(r);
		}
		rows.sort(function (a, b) {
			if (a.start !== b.start) {
				return a.start - b.start;
			}
			return b.end - a.end;
		});
		return rows;
	}

	function normalizeInfoType(row) {
		return String(row && row.info_type != null ? row.info_type : "")
			.trim()
			.toLowerCase();
	}

	function spansOverlapRows(a, b) {
		var as = Math.max(0, Math.floor(Number(a.start)));
		var ae = Math.max(0, Math.floor(Number(a.end)));
		var bs = Math.max(0, Math.floor(Number(b.start)));
		var be = Math.max(0, Math.floor(Number(b.end)));
		if (!(ae > as && be > bs)) {
			return false;
		}
		return Math.max(as, bs) < Math.min(ae, be);
	}

	function typesMatchRow(a, b) {
		return normalizeInfoType(a) === normalizeInfoType(b);
	}

	function rowHasTwinInSiblingList(row, siblingList) {
		if (!siblingList || !siblingList.length) {
			return false;
		}
		for (var si = 0; si < siblingList.length; si++) {
			var o = siblingList[si];
			if (typesMatchRow(row, o) && spansOverlapRows(row, o)) {
				return true;
			}
		}
		return false;
	}

	function addBreakpointsFromRows(bodyLen, rows, accSet) {
		for (var i = 0; i < rows.length; i++) {
			var rs = rows[i];
			var s = Math.max(0, Math.min(bodyLen, Math.floor(Number(rs.start))));
			var e = Math.max(0, Math.min(bodyLen, Math.floor(Number(rs.end))));
			if (e > s) {
				accSet.add(s);
				accSet.add(e);
			}
		}
	}

	function segmentTouchedByRows(s, e, rows, bodyLen) {
		for (var j = 0; j < rows.length; j++) {
			var r = rows[j];
			var rs = Math.max(0, Math.floor(Number(r.start)));
			var re = Math.max(0, Math.floor(Number(r.end)));
			if (re <= rs || rs >= bodyLen) {
				continue;
			}
			if (rs < e && re > s) {
				return true;
			}
		}
		return false;
	}

	function inferTypeForSegment(mid, mergedAgent, touchedA, mergedDet, touchedB) {
		if (touchedA) {
			for (var ia = 0; ia < mergedAgent.length; ia++) {
				var ra = mergedAgent[ia];
				var as = Math.max(0, Math.floor(Number(ra.start)));
				var ex = Math.max(0, Math.floor(Number(ra.end)));
				if (ex > as && mid >= as && mid < ex) {
					return ra.info_type;
				}
			}
		}
		if (touchedB) {
			for (var id = 0; id < mergedDet.length; id++) {
				var rb = mergedDet[id];
				var bs = Math.max(0, Math.floor(Number(rb.start)));
				var bx = Math.max(0, Math.floor(Number(rb.end)));
				if (bx > bs && mid >= bs && mid < bx) {
					return rb.info_type;
				}
			}
		}
		if (touchedA && mergedAgent.length) {
			return mergedAgent[0].info_type;
		}
		if (touchedB && mergedDet.length) {
			return mergedDet[0].info_type;
		}
		return "";
	}

	var boot = readBoot();
	var ui = boot.uiStrings || {};
	var examples = boot.examples || [];

	function stripLookup(key, fb) {
		var raw = ui[key];
		return raw != null && String(raw).trim() !== "" ? String(raw) : fb;
	}

	function renderCombinedHighlightedBody(container, body, clippedAgentSpans, clippedDetSpans) {
		container.innerHTML = "";
		if (!body) {
			return;
		}
		var bodyLen = body.length;
		var mergedA = Array.isArray(clippedAgentSpans) ? clippedAgentSpans : [];
		var mergedB = Array.isArray(clippedDetSpans) ? clippedDetSpans : [];
		var bpSet = new Set();
		bpSet.add(0);
		bpSet.add(bodyLen);
		addBreakpointsFromRows(bodyLen, mergedA, bpSet);
		addBreakpointsFromRows(bodyLen, mergedB, bpSet);
		var bps = Array.from(bpSet).sort(function (x, y) {
			return x - y;
		});
		var cursor = 0;
		for (var bi = 0; bi < bps.length - 1; bi++) {
			var segS = Math.max(0, bps[bi]);
			var segE = Math.min(bodyLen, bps[bi + 1]);
			if (segE <= segS) {
				continue;
			}
			if (cursor < segS) {
				container.appendChild(document.createTextNode(body.slice(cursor, segS)));
			}
			var inA = segmentTouchedByRows(segS, segE, mergedA, bodyLen);
			var inD = segmentTouchedByRows(segS, segE, mergedB, bodyLen);
			if (!inA && !inD) {
				container.appendChild(document.createTextNode(body.slice(segS, segE)));
				cursor = segE;
				continue;
			}
			var mid = Math.floor((segS + segE) / 2);
			var typ = inferTypeForSegment(mid, mergedA, inA, mergedB, inD);
			var mk = document.createElement("mark");
			mk.className = "lpl-mark " + typeCssClass(typ);
			if (inA && inD) {
				mk.className += " lpl-match-both";
				mk.setAttribute(
					"title",
					String(typ || "") +
						" · " +
						stripLookup("matchBadgeBothTitle", "Both methods")
				);
			} else {
				mk.className += " lpl-match-exclusive";
				mk.setAttribute(
					"title",
					String(typ || "") +
						" · " +
						stripLookup("matchBadgeExclusiveTitle", "One method only")
				);
			}
			mk.appendChild(document.createTextNode(body.slice(segS, segE)));
			container.appendChild(mk);
			cursor = segE;
		}
		if (cursor < bodyLen) {
			container.appendChild(document.createTextNode(body.slice(cursor)));
		}
	}

	function renderPiiListWithMatchBadges(root, extractions, siblingExtractions, uiObj) {
		if (!root) {
			return;
		}
		root.innerHTML = "";
		var rangePat = uiObj.piiItemRange || "{start}–{end}";
		var rows = Array.isArray(extractions) ? extractions : [];
		var sibling = Array.isArray(siblingExtractions) ? siblingExtractions : [];
		if (!rows.length) {
			return;
		}
		for (var i = 0; i < rows.length; i++) {
			var row = rows[i];
			var both = rowHasTwinInSiblingList(row, sibling);
			var details = document.createElement("details");
			var summary = document.createElement("summary");
			var rowWrap = document.createElement("span");
			rowWrap.className = "lpl-pii-summary-row";
			var badge = document.createElement("span");
			badge.className = both
				? "lpl-match-badge lpl-match-badge-both"
				: "lpl-match-badge lpl-match-badge-exclusive";
			badge.textContent = stripLookup(
				both ? "matchBadgeBothLabel" : "matchBadgeExclusiveLabel",
				both ? "Both" : "One only"
			);
			badge.setAttribute(
				"title",
				stripLookup(
					both ? "matchBadgeBothTitle" : "matchBadgeExclusiveTitle",
					both ? "Matched in both lists" : "Unique to this list"
				)
			);
			var lbl = document.createElement("span");
			lbl.className = "lpl-pii-summary-text";
			var shortVal = String(row.info_value || "").trim();
			if (shortVal.length > 48) shortVal = shortVal.slice(0, 45) + "…";
			lbl.textContent = (row.info_type || "") + " · " + shortVal;
			rowWrap.appendChild(badge);
			rowWrap.appendChild(lbl);
			summary.appendChild(rowWrap);
			var bodyInner = document.createElement("div");
			bodyInner.className = "lpl-pii-body";
			var rangeLine = document.createElement("div");
			rangeLine.textContent = format(rangePat, {
				start: row.start,
				end: row.end,
			});
			var valueLine = document.createElement("div");
			valueLine.textContent = String(row.info_value || "");
			bodyInner.appendChild(rangeLine);
			bodyInner.appendChild(valueLine);
			details.appendChild(summary);
			details.appendChild(bodyInner);
			root.appendChild(details);
		}
	}

	function clearUsageDetails() {
		if (usageWrap) {
			usageWrap.hidden = true;
			usageWrap.open = false;
		}
		if (usagePre) {
			usagePre.textContent = "";
		}
	}

	function renderUsageDetails(bundle) {
		var F =
			typeof window.EmailPiiLabFormat === "object" &&
			window.EmailPiiLabFormat !== null
				? window.EmailPiiLabFormat
				: {};
		var fts =
			typeof F.formatTokenCountSuffix === "function"
				? F.formatTokenCountSuffix
				: function (x) {
						return String(x);
					};
		var fad =
			typeof F.formatAssistantDuration === "function"
				? F.formatAssistantDuration
				: function () {
						return "";
					};
		var stringifyTokenDetails =
			typeof F.stringifyTokenDetails === "function"
				? F.stringifyTokenDetails
				: function (obj) {
						return JSON.stringify(obj, null, 2);
					};

		if (!usageWrap || !usagePre) return;
		var hasAnything =
			bundle &&
			(bundle.usage ||
				bundle.durationMs != null ||
				bundle.promptTokenBreakdown ||
				bundle.model);
		if (!hasAnything) {
			clearUsageDetails();
			return;
		}

		var lines = [];

		function LL(k, fb) {
			return stripLookup(k, fb);
		}

		var br = bundle.promptTokenBreakdown;
		if (br && typeof br === "object" && Array.isArray(br.rows) && br.rows.length > 0) {
			lines.push(LL("usagePromptBreakdownTitle", "Estimated prompt (before model)"));
			for (var ri = 0; ri < br.rows.length; ri++) {
				var row = br.rows[ri];
				var label = row.usageLabel != null ? String(row.usageLabel) : "";
				var estTok = Number(row.estimatedTokens);
				var tokStr = fts(Number.isFinite(estTok) ? estTok : 0);
				lines.push(
					String(LL("usagePreviewRow", "{role} · ~{n} tok (est.)"))
						.replace("{role}", label)
						.replace("{n}", tokStr)
				);
			}
			var totalEst = Number(br.estimatedPromptTokens);
			if (Number.isFinite(totalEst)) {
				lines.push(
					String(LL("usagePromptSubtotal", "Prompt subtotal · ~{n} tok (est.)")).replace(
						"{n}",
						fts(totalEst)
					)
				);
			}
			if (br.tokenEstimateNote && String(br.tokenEstimateNote).trim() !== "") {
				lines.push(String(br.tokenEstimateNote).trim());
			}
		}

		if (bundle.model) {
			lines.push(LL("usageModelLabel", "Model") + ": " + String(bundle.model));
		}

		if (bundle.durationMs != null) {
			var d = fad(bundle.durationMs, function lk(k, fb) {
				return LL(k, fb);
			});
			if (d) {
				lines.push(LL("usageTimeLabel", "Time") + ": " + d);
			}
		}

		if (bundle.usage) {
			var u = bundle.usage;
			if (u.input_tokens != null) {
				lines.push(LL("usageInTokens", "Input tokens") + ": " + fts(u.input_tokens));
			}
			if (u.output_tokens != null) {
				lines.push(LL("usageOutTokens", "Output tokens") + ": " + fts(u.output_tokens));
			}
			if (u.total_tokens != null) {
				lines.push(LL("usageTotalTokens", "Total tokens") + ": " + fts(u.total_tokens));
			}
			if (u.completion_tokens_details) {
				lines.push(stringifyTokenDetails(u.completion_tokens_details));
			}
		}

		usagePre.textContent = lines.join("\n");
		usageWrap.hidden = false;
	}

	function el(tag, className, text) {
		var n = document.createElement(tag);
		if (className) n.className = className;
		if (text != null) n.textContent = text;
		return n;
	}

	var nav = document.getElementById("lpl-example-nav");
	var bodyPane = document.getElementById("lpl-body-pane");
	var extractBtn = document.getElementById("lpl-extract");
	var extractDeterministicBtn = document.getElementById("lpl-extract-deterministic");
	var clearAgentPaneBtn = document.getElementById("lpl-clear-agent-pane");
	var clearDeterministicPaneBtn = document.getElementById("lpl-clear-deterministic-pane");
	var statusEl = document.getElementById("lpl-status");
	var piiRootAgent = document.getElementById("lpl-pii-root-agent");
	var piiRootDeterministic = document.getElementById("lpl-pii-root-deterministic");
	var metaEl = document.getElementById("lpl-meta");
	var usageWrap = document.getElementById("lpl-usage-meta");
	var usagePre = document.getElementById("lpl-usage-pre");
	var runIndicator = document.getElementById("lpl-run-indicator");
	var runElapsed = document.getElementById("lpl-run-elapsed");
	var runLastLog = document.getElementById("lpl-run-lastlog");
	var logPanel = document.getElementById("lpl-log-panel");
	var logPre = document.getElementById("lpl-log-pre");
	var logSummary = document.getElementById("lpl-log-summary");

	var progressPollIv = null;
	var pollInvalidateToken = 0;
	var agentRunInFlight = false;
	var deterministicRunInFlight = false;

	function invalidateExtractionPolling() {
		stopProgressPoll();
		pollInvalidateToken += 1;
		agentRunInFlight = false;
		deterministicRunInFlight = false;
		syncRunChrome();
	}

	function stopProgressPoll() {
		if (progressPollIv !== null) {
			clearInterval(progressPollIv);
			progressPollIv = null;
		}
	}

	function hideRunIndicator() {
		if (runIndicator) {
			runIndicator.hidden = true;
		}
		if (runElapsed) {
			runElapsed.textContent = "";
		}
		if (runLastLog) {
			runLastLog.textContent = "";
		}
	}

	function resetLogPanel() {
		if (logPre) {
			logPre.textContent = "";
		}
		if (logPanel) {
			logPanel.hidden = true;
			logPanel.open = false;
		}
		if (logSummary) {
			logSummary.textContent = stripLookup("extractionLogsSummary", "Extraction logs");
		}
	}

	function showRunIndicator() {
		if (runIndicator) {
			runIndicator.hidden = false;
		}
	}

	function truncateLine(text, maxLen) {
		var t = String(text || "");
		if (t.length <= maxLen) {
			return t;
		}
		return t.slice(0, Math.max(0, maxLen - 1)) + "\u2026";
	}

	function applyProgressSnapshot(p) {
		if (!p || !p.ok) {
			return;
		}
		var secs = Number(p.elapsed_seconds);
		if (!Number.isFinite(secs)) {
			secs = 0;
		}
		var labelTpl = stripLookup("elapsedRunningLabel", "{n}s");
		if (runElapsed) {
			runElapsed.textContent = labelTpl.replace("{n}", secs.toFixed(1));
		}
		if (runLastLog) {
			runLastLog.textContent = truncateLine(p.last_line || "", 140);
		}
		if (logPre && Array.isArray(p.lines)) {
			logPre.textContent = p.lines.join("\n");
			logPre.scrollTop = logPre.scrollHeight;
		}
		if (logPanel) {
			logPanel.hidden = false;
		}
		if (logSummary) {
			var nlines = Array.isArray(p.lines) ? p.lines.length : 0;
			var tpl = stripLookup("logSummaryLineCount", "Extraction logs ({n})");
			logSummary.textContent = tpl.replace("{n}", String(nlines));
		}
	}

	function refreshActiveView() {
		var agentExtrRaw =
			lastAgentResult && Array.isArray(lastAgentResult.extractions)
				? lastAgentResult.extractions
				: [];
		var detExtrRaw = Array.isArray(lastDeterministicExtractions)
			? lastDeterministicExtractions
			: [];
		var agentExtr = clipExtractionsForBody(currentBody, agentExtrRaw);
		var detExtr = clipExtractionsForBody(currentBody, detExtrRaw);
		renderCombinedHighlightedBody(bodyPane, currentBody, agentExtr, detExtr);
		renderPiiListWithMatchBadges(piiRootAgent, agentExtr, detExtr, ui);
		renderPiiListWithMatchBadges(piiRootDeterministic, detExtr, agentExtr, ui);
		if (lastAgentResult) {
			renderUsageDetails({
				usage: lastAgentResult.usage,
				durationMs: lastAgentResult.durationMs,
				promptTokenBreakdown: lastAgentResult.promptTokenBreakdown,
				model: lastAgentResult.model,
			});
		} else {
			clearUsageDetails();
		}
	}

	function applyAgentResultBundle(bundle, opts) {
		opts = opts || {};
		if (!bundle || typeof bundle !== "object") {
			lastAgentResult = null;
		} else {
			lastAgentResult = bundle;
			if (!Array.isArray(lastAgentResult.extractions)) {
				lastAgentResult.extractions = [];
			}
		}
		refreshActiveView();
		if (!opts.suppressClearStatusLine) {
			updateIdleGuideStatus();
		}
	}

	function beginProgressPolling(jobId, onDone) {
		stopProgressPoll();
		var receipt = ++pollInvalidateToken;
		var pollingDone = false;
		function doneOnce() {
			if (pollingDone) {
				return;
			}
			pollingDone = true;
			if (typeof onDone === "function") {
				onDone();
			}
		}
		showRunIndicator();
		resetLogPanel();
		if (logPanel) {
			logPanel.hidden = false;
		}
		syncRunChrome();

		function tick() {
			fetch("/api/extract/progress?job_id=" + encodeURIComponent(jobId))
				.then(function (res) {
					return res.json().then(function (data) {
						return { ok: res.ok, status: res.status, data: data };
					});
				})
				.then(function (pack) {
					if (receipt !== pollInvalidateToken) {
						return;
					}
					var data = pack.data || {};
					if (!pack.ok || !data.ok) {
						stopProgressPoll();
						var err = data.error || "poll_failed";
						throw new Error(err);
					}
					if (receipt !== pollInvalidateToken) {
						return;
					}
					applyProgressSnapshot(data);
					if (data.status === "complete") {
						stopProgressPoll();
						if (receipt !== pollInvalidateToken) {
							return;
						}
						var extracted = data.result || {};
						applyAgentResultBundle(extracted);
						rememberLatestExtractionForCurrentExample(extracted);
						hideRunIndicator();
						doneOnce();
					} else if (data.status === "error") {
						stopProgressPoll();
						if (receipt !== pollInvalidateToken) {
							return;
						}
						var detailMsg = data.detail || data.error || "extraction_failed";
						setStatus((ui.errorPrefix || "Error") + ": " + detailMsg, true);
						lastAgentResult = null;
						refreshActiveView();
						hideRunIndicator();
						doneOnce();
					}
				})
				.catch(function (err) {
					stopProgressPoll();
					if (receipt !== pollInvalidateToken) {
						return;
					}
					setStatus((ui.errorPrefix || "Error") + ": " + (err.message || String(err)), true);
					lastAgentResult = null;
					refreshActiveView();
					hideRunIndicator();
					doneOnce();
				});
		}

		tick();
		progressPollIv = setInterval(tick, 250);
	}

	var currentId = null;
	var currentBody = "";
	var lastAgentResult = null;
	var lastDeterministicExtractions = [];

	/** Previously used by localStorage migration into ``local_cache``. */
	var EXTRACTION_LS_LEGACY_KEY = "emailPiiLab.extraction_cache_v1";
	var EXTRACTION_DET_LS_LEGACY_KEY = "emailPiiLab.deterministic_cache_v1";

	function fingerprintEmailBody(body) {
		var s = String(body || "");
		var h = 5381;
		for (var fi = 0; fi < s.length; fi += 1) {
			h = ((h << 5) + h + s.charCodeAt(fi)) >>> 0;
		}
		return s.length + ":" + String(h);
	}

	function parseJsonMaybeObject(raw) {
		try {
			if (!raw || typeof raw !== "string") {
				return null;
			}
			var parsed = JSON.parse(raw);
			return parsed !== null &&
				typeof parsed === "object" &&
				!Array.isArray(parsed)
				? parsed
				: null;
		} catch (ex) {
			return null;
		}
	}

	function coerceStoreInbound(raw) {
		if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
			return {};
		}
		var result = {};
		for (var key in raw) {
			if (
				Object.prototype.hasOwnProperty.call(raw, key) &&
				raw[key] !== null &&
				typeof raw[key] === "object" &&
				!Array.isArray(raw[key])
			) {
				result[String(key)] = raw[key];
			}
		}
		return result;
	}

	var extractionCacheStore = {};
	var deterministicCacheStore = {};

	function putPiiCaches(chunks) {
		var writeBody = chunks || {};
		if (!writeBody.agent_store && !writeBody.deterministic_store) {
			return Promise.resolve(null);
		}
		return fetch("/api/cache/pii-stores", {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(writeBody),
		}).then(function (res) {
			return res.json().then(function (data) {
				if (!res.ok || !data || !data.ok) {
					var hint = data && data.error ? String(data.error) : "unknown";
					console.warn(
						"[email_pii_lab] disk cache PUT failed:",
						res.status,
						hint
					);
				}
			});
		}).catch(function (netErr) {
			console.warn("[email_pii_lab] disk cache PUT network error", netErr);
		});
	}

	function migrateLegacyCachesFromBrowserStorageIntoMemory() {
		var mutatedLabels = [];
		if (
			typeof Storage === "undefined" ||
			typeof localStorage === "undefined"
		) {
			return mutatedLabels;
		}
		try {
			if (!Object.keys(extractionCacheStore).length) {
				var legacyA = parseJsonMaybeObject(
					localStorage.getItem(EXTRACTION_LS_LEGACY_KEY)
				);
				if (legacyA) {
					extractionCacheStore = coerceStoreInbound(legacyA);
					localStorage.removeItem(EXTRACTION_LS_LEGACY_KEY);
					mutatedLabels.push("agent");
				}
			}
			if (!Object.keys(deterministicCacheStore).length) {
				var legacyB = parseJsonMaybeObject(
					localStorage.getItem(EXTRACTION_DET_LS_LEGACY_KEY)
				);
				if (legacyB) {
					deterministicCacheStore = coerceStoreInbound(legacyB);
					localStorage.removeItem(EXTRACTION_DET_LS_LEGACY_KEY);
					mutatedLabels.push("det");
				}
			}
		} catch (mEx) {
			console.warn("[email_pii_lab] legacy localStorage migrate failed", mEx);
		}
		return mutatedLabels;
	}

	function persistAfterLegacyMigrate(labels) {
		var bodyParts = {};
		if (labels.indexOf("agent") >= 0) {
			bodyParts.agent_store = extractionCacheStore;
		}
		if (labels.indexOf("det") >= 0) {
			bodyParts.deterministic_store = deterministicCacheStore;
		}
		if (!Object.keys(bodyParts).length) {
			return Promise.resolve(null);
		}
		return putPiiCaches(bodyParts);
	}

	function hydrateCachesFromDisk() {
		function applyStores(agentS, detS) {
			extractionCacheStore = coerceStoreInbound(agentS);
			deterministicCacheStore = coerceStoreInbound(detS);
		}

		function finalizeDiskLoad() {
			var migrated = migrateLegacyCachesFromBrowserStorageIntoMemory();
			return persistAfterLegacyMigrate(migrated);
		}

		return fetch("/api/cache/pii-stores")
			.then(function (diskRes) {
				return diskRes.json().then(function (data) {
					return {
						ok: diskRes.ok && data && data.ok,
						raw: data,
					};
				});
			})
			.then(function (packed) {
				if (!packed.ok || !packed.raw) {
					throw new Error("pii_disk_read_failed");
				}
				applyStores(packed.raw.agent_store, packed.raw.deterministic_store);
				return finalizeDiskLoad();
			})
			.catch(function (hydErr) {
				console.warn(
					"[email_pii_lab] disk cache hydrate failed — trying legacy browser caches only.",
					hydErr
				);
				applyStores(null, null);
				return finalizeDiskLoad();
			});
	}

	function persistAgentStoreDisk() {
		putPiiCaches({ agent_store: extractionCacheStore });
	}

	function persistDeterministicStoreDisk() {
		putPiiCaches({ deterministic_store: deterministicCacheStore });
	}

	function deterministicCachePut(exampleId, emailBody, envelope) {
		if (!envelope || typeof envelope !== "object") {
			return;
		}
		var blob = cloneSerializable(envelope);
		if (blob == null) {
			return;
		}
		var sidex = String(exampleId);
		deterministicCacheStore[sidex] = {
			bodyFingerprint: fingerprintEmailBody(emailBody),
			result: blob,
		};
		persistDeterministicStoreDisk();
	}

	function deterministicCacheGet(exampleId, emailBody) {
		var prow = deterministicCacheStore[String(exampleId)];
		if (!prow || typeof prow !== "object") {
			return null;
		}
		if (prow.bodyFingerprint !== fingerprintEmailBody(emailBody)) {
			return null;
		}
		return cloneSerializable(prow.result);
	}

	function cloneSerializable(value) {
		try {
			return JSON.parse(JSON.stringify(value));
		} catch (e) {
			return null;
		}
	}

	function extractionCachePut(exampleId, emailBody, result) {
		if (result == null) {
			return;
		}
		var blob = cloneSerializable(result);
		if (blob == null) {
			return;
		}
		var sid = String(exampleId);
		extractionCacheStore[sid] = {
			bodyFingerprint: fingerprintEmailBody(emailBody),
			result: blob,
		};
		persistAgentStoreDisk();
	}

	function extractionCacheGet(exampleId, emailBody) {
		var row = extractionCacheStore[String(exampleId)];
		if (!row || typeof row !== "object") {
			return null;
		}
		if (row.bodyFingerprint !== fingerprintEmailBody(emailBody)) {
			return null;
		}
		return cloneSerializable(row.result);
	}

	function rememberLatestExtractionForCurrentExample(payload) {
		if (!currentId || !payload) {
			return;
		}
		extractionCachePut(currentId, currentBody, payload);
	}

	function clearAgentPaneForSelection() {
		if (currentId == null || String(currentId).length === 0) {
			return;
		}
		if (agentRunInFlight || deterministicRunInFlight) {
			return;
		}
		var cid = String(currentId);
		if (!extractionCacheStore[cid]) {
			return;
		}
		delete extractionCacheStore[cid];
		lastAgentResult = null;
		persistAgentStoreDisk();
		refreshActiveView();
		syncRunChrome();
		updateIdleGuideStatus();
	}

	function clearDeterministicPaneForSelection() {
		if (currentId == null || String(currentId).length === 0) {
			return;
		}
		if (agentRunInFlight || deterministicRunInFlight) {
			return;
		}
		var cid2 = String(currentId);
		if (!deterministicCacheStore[cid2]) {
			return;
		}
		delete deterministicCacheStore[cid2];
		lastDeterministicExtractions = [];
		persistDeterministicStoreDisk();
		refreshActiveView();
		syncRunChrome();
		updateIdleGuideStatus();
	}

	function setStatus(msg, isError) {
		statusEl.textContent = msg || "";
		statusEl.classList.toggle("is-error", Boolean(isError));
	}

	function syncClearPaneButtons() {
		if (!clearAgentPaneBtn && !clearDeterministicPaneBtn) {
			return;
		}
		var sid =
			currentId != null && String(currentId).length > 0
				? String(currentId)
				: "";
		var blocked = deterministicRunInFlight || agentRunInFlight;
		if (clearAgentPaneBtn) {
			clearAgentPaneBtn.disabled =
				!sid ||
				blocked ||
				!Object.prototype.hasOwnProperty.call(extractionCacheStore, sid);
		}
		if (clearDeterministicPaneBtn) {
			clearDeterministicPaneBtn.disabled =
				!sid ||
				blocked ||
				!Object.prototype.hasOwnProperty.call(deterministicCacheStore, sid);
		}
	}

	function syncRunChrome() {
		if (!extractBtn) {
			syncClearPaneButtons();
			return;
		}
		var busyAgent = stripLookup("extractBusyLabel", "Running…");
		var idleAgent = stripLookup("extractButtonLabel", "Run extraction");
		extractBtn.disabled =
			!currentBody || deterministicRunInFlight || agentRunInFlight;
		extractBtn.textContent = agentRunInFlight ? busyAgent : idleAgent;
		if (!extractDeterministicBtn) {
			syncClearPaneButtons();
			return;
		}
		var busyDet = stripLookup("deterministicExtractBusyLabel", "Scanning…");
		var idleDet = stripLookup("deterministicExtractLabel", "Run deterministic");
		extractDeterministicBtn.disabled =
			!currentBody || deterministicRunInFlight || agentRunInFlight;
		extractDeterministicBtn.textContent = deterministicRunInFlight ? busyDet : idleDet;
		syncClearPaneButtons();
	}

	function updateIdleGuideStatus() {
		if (!currentBody) {
			return;
		}
		var bothReadyHint = stripLookup(
			"statusBothPanesReady",
			"Agent and deterministic lists are populated."
		);
		var agentOnlyHint = stripLookup(
			"statusAgentOnlyReady",
			"Agent results shown; run deterministic to compare."
		);
		var deterministicOnlyHint = stripLookup(
			"statusDeterministicOnlyReady",
			"Deterministic results shown; run extraction to compare."
		);
		var neitherHint = stripLookup(
			"statusBothPanesEmpty",
			"Run extraction and/or deterministic scan to populate both side panes."
		);
		var na =
			lastAgentResult && Array.isArray(lastAgentResult.extractions)
				? lastAgentResult.extractions.length
				: 0;
		var hasAgentUsageBundle =
			lastAgentResult &&
			(lastAgentResult.usage ||
				lastAgentResult.durationMs != null ||
				lastAgentResult.promptTokenBreakdown ||
				lastAgentResult.model);
		var ndRows = Array.isArray(lastDeterministicExtractions)
			? lastDeterministicExtractions.length
			: 0;
		var hasAgent = na > 0 || hasAgentUsageBundle;
		var hasDet = ndRows > 0;
		if (hasAgent && hasDet) {
			setStatus(bothReadyHint, false);
		} else if (hasAgent) {
			setStatus(agentOnlyHint, false);
		} else if (hasDet) {
			setStatus(deterministicOnlyHint, false);
		} else {
			setStatus(neitherHint, false);
		}
	}

	function showMeta(ex) {
		if (!ex) {
			metaEl.hidden = true;
			metaEl.innerHTML = "";
			return;
		}
		metaEl.hidden = false;
		metaEl.innerHTML = "";
		var idLine = el("div", null, (ui.exampleIdLabel || "Id") + ": " + String(ex.id || ""));
		var subLine = el("div", null, (ui.subjectLabel || "Subject") + ": " + String(ex.subject || ""));
		metaEl.appendChild(idLine);
		metaEl.appendChild(subLine);
	}

	function selectExample(id) {
		invalidateExtractionPolling();
		hideRunIndicator();
		resetLogPanel();
		currentId = id;
		var ex = null;
		for (var i = 0; i < examples.length; i++) {
			if (String(examples[i].id) === id) ex = examples[i];
		}
		currentBody = ex && typeof ex.body === "string" ? ex.body : "";
		showMeta(ex);

		var buttonsEarly = nav.querySelectorAll(".lpl-example-btn");
		for (var be = 0; be < buttonsEarly.length; be++) {
			buttonsEarly[be].classList.toggle("is-active", buttonsEarly[be].dataset.exampleId === id);
		}

		if (!currentBody) {
			lastAgentResult = null;
			lastDeterministicExtractions = [];
			refreshActiveView();
			syncRunChrome();
			setStatus(ui.noExampleSelected || "", false);
			return;
		}

		var envelopeAgent = extractionCacheGet(id, currentBody);
		lastAgentResult = envelopeAgent || null;
		var envelopeDet = deterministicCacheGet(id, currentBody);
		lastDeterministicExtractions =
			envelopeDet && Array.isArray(envelopeDet.extractions) ? envelopeDet.extractions : [];
		refreshActiveView();
		syncRunChrome();
		updateIdleGuideStatus();
	}

	function buildNav() {
		nav.innerHTML = "";
		for (var i = 0; i < examples.length; i++) {
			var ex = examples[i];
			var id = String(ex.id != null ? ex.id : i);
			var btn = el("button", "lpl-example-btn");
			btn.type = "button";
			btn.dataset.exampleId = id;
			var title = el("span", null, String(ex.subject || id));
			var sub = el("span", "lpl-ex-id", id);
			btn.appendChild(title);
			btn.appendChild(sub);
			btn.addEventListener("click", function (ev) {
				var bid = ev.currentTarget.dataset.exampleId;
				selectExample(bid);
			});
			nav.appendChild(btn);
		}
	}

	extractBtn.textContent = ui.extractButtonLabel || "Run extraction";
	if (extractDeterministicBtn) {
		extractDeterministicBtn.textContent =
			ui.deterministicExtractLabel || "Run deterministic";
	}

	if (clearAgentPaneBtn) {
		clearAgentPaneBtn.addEventListener("click", function () {
			clearAgentPaneForSelection();
		});
	}
	if (clearDeterministicPaneBtn) {
		clearDeterministicPaneBtn.addEventListener("click", function () {
			clearDeterministicPaneForSelection();
		});
	}

	extractBtn.addEventListener("click", function () {
		if (!currentId) return;
		agentRunInFlight = true;
		syncRunChrome();
		setStatus("", false);
		fetch("/api/extract", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ example_id: currentId }),
		})
			.then(function (res) {
				return res.json().then(function (data) {
					return { ok: res.ok, status: res.status, data: data };
				});
			})
			.then(function (pack) {
				var data = pack.data || {};
				if (!pack.ok || !data.ok) {
					var err = data.error || "request_failed";
					var extra = "";
					if (data.detail) {
						extra = " — " + data.detail;
					} else if (
						pack.status === 503 &&
						err === "missing_openai_api_key" &&
						ui.missingApiKeyHint
					) {
						extra = " — " + ui.missingApiKeyHint;
					}
					throw new Error(err + extra);
				}
				var jid = data.job_id;
				if (!jid) {
					throw new Error("missing_job_id");
				}
				beginProgressPolling(String(jid), function agentPollFinished() {
					agentRunInFlight = false;
					syncRunChrome();
				});
			})
			.catch(function (e) {
				agentRunInFlight = false;
				var prefix = ui.errorPrefix || "Error";
				setStatus(prefix + ": " + (e.message || String(e)), true);
				lastAgentResult = null;
				refreshActiveView();
				syncRunChrome();
			});
	});

	if (extractDeterministicBtn) {
		extractDeterministicBtn.addEventListener("click", function () {
			if (!currentId || !currentBody) {
				return;
			}
			var Mod =
				typeof window.EmailPiiLabDeterministic === "object" &&
				window.EmailPiiLabDeterministic !== null
					? window.EmailPiiLabDeterministic
					: null;
			var scanFn =
				Mod !== null &&
				Object.prototype.hasOwnProperty.call(Mod, "scan") &&
				typeof Mod.scan === "function"
					? Mod.scan
					: null;
			if (!scanFn) {
				setStatus((ui.errorPrefix || "Error") + ": deterministic_scan_unavailable", true);
				return;
			}
			deterministicRunInFlight = true;
			syncRunChrome();
			setStatus("", false);
			var rows = [];
			try {
				var scanned = scanFn.call(Mod, currentBody);
				rows = Array.isArray(scanned) ? scanned : [];
			} catch (scanErr) {
				setStatus(
					(ui.errorPrefix || "Error") + ": " + (scanErr.message || String(scanErr)),
					true
				);
				deterministicRunInFlight = false;
				syncRunChrome();
				return;
			}
			lastDeterministicExtractions = rows;
			deterministicCachePut(currentId, currentBody, { extractions: rows });
			deterministicRunInFlight = false;
			syncRunChrome();
			refreshActiveView();
			updateIdleGuideStatus();
		});
	}

	buildNav();

	function bootAfterCachesReady() {
		if (!examples.length) {
			setStatus(ui.noExampleSelected || "", true);
			syncRunChrome();
			return;
		}
		selectExample(String(examples[0].id != null ? examples[0].id : "0"));
	}

	hydrateCachesFromDisk().then(bootAfterCachesReady);
})();
