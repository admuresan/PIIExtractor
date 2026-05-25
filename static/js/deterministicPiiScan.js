/**
 * Read genai/toolkits/architectural_instructions before making changes.
 *
 * Lightweight, dependency-free deterministic PII span finder for the Email PII lab.
 * All candidate patterns must pass keyword/context gates (except label-led captures).
 * Output rows match Flask agent shape: { start, end, info_type, info_value }.
 */

(function defineEmailPiiLabDeterministic(global) {
	var TYPE_PHONE = "phone number";
	var TYPE_ADDRESS = "address";
	var TYPE_DOB = "date of birth";
	var TYPE_NAME = "name";

	/* -----------------------------------------------------------------------------
	 * CONTEXT & KEYWORDS (paired with regexes below; tune together)
	 * -------------------------------------------------------------------------- */

	/** Chars sampled before each candidate span (Unicode code units, same indexing as Flask body slice). */
	var CONTEXT_CHARS_BEFORE = 120;
	/** Chars sampled after the span. Combined horizon ~240 + span length across the hotspot. */
	var CONTEXT_CHARS_AFTER = 140;

	var KEYWORDS_DOB = [
		"dob",
		"date of birth",
		"birth date",
		"birthdate",
		"birthday",
		"birth year",
		"born ",
		"born:",
	];

	var KEYWORDS_PHONE = [
		"phone",
		"toll",
		"mobile",
		"pager",
		"fax",
		"tel",
		"sms",
		"direct line",
		"direct:",
		"call me",
		"reachable",
		"dial ",
	];

	var KEYWORDS_ADDRESS = [
		"address",
		"ship ",
		"shipping",
		"deliver",
		"delivery",
		"march to",
		"mail ",
		"mailing",
		"billing",
		"route",
		" attn",
		"attn:",
		" attn.",
		"street",
		"real delivery",
		"primary:",
	];

	/** If any of these appear in the hotspot and the subtype is heuristic (not explicit label-led), discard. */
	var DISALLOW_LOOSE_HIT = [
		"fictional",
		"famous fake",
		"famously fake",
		"treat as trivia",
		"ignore ",
		"ignore.",
		"ignore that",
		"don't extract",
		"don’t extract",
		"joke ",
		" gag ",
		"placeholder:",
		"placeholder line",
		" no hidden ",
	];

	/** DOB hotspot must not suggest a missing placeholder value. */
	var DISALLOW_DOB_SPECIFIC = ["tbd", "n/a", "not applicable"];

	/* -----------------------------------------------------------------------------
	 * REGEX CONSTANTS — candidate patterns (tune carefully)
	 *
	 * Every RegExp literal used for scans lives below. Prefer `g` flags for iterators.
	 * Keep comments in sync when changing keyword/disallow lists above.
	 * -------------------------------------------------------------------------- */

	/*
	 * DOB_ISO: Strict YYYY-MM-DD (digits + hyphens).
	 * Coupled with KEYWORDS_DOB + DISALLOW_* in gateDobCandidate.
	 * Flags: global for repeated exec scans; anchored word boundaries kill mid-token noise where possible.
	 */
	var RE_DOB_ISO = /\b\d{4}-\d{2}-\d{2}\b/g;

	/*
	 * DOB_MDY_SLASH_OR_DASH: M/D/Y or MM-DD-YYYY style with / or -
	 * Intentionally conservative on month/day bounds in-regex; rejects obvious bad later optional.
	 * Risk: numeric triples elsewhere (invoice lines) suppressed by DISALLOW + DOB keywords only nearby.
	 */
	var RE_DOB_MDY_SLASH_OR_DASH = /\b(?:0?[1-9]|1[0-2])[\/\-](?:0?[1-9]|[12]\d|3[01])[\/\-](?:\d{2}|\d{4})\b/g;

	/*
	 * DOB_DMY_DOT: European dotted dates (also matches some typo variants in fixtures).
	 */
	var RE_DOB_DMY_DOT = /\b(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?:\d{2}|\d{4})\b/g;

	/*
	 * DOB_MONTH_NAME: English month prefixes with day + year ("March 14, 1991").
	 * Case-insensitive; allows optional comma before year.
	 */
	var RE_DOB_MONTH_NAME =
		/\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}\b/gi;

	/*
	 * PHONE_E164-ish US: Leading +1 then flexible separators to 10 digits.
	 */
	var RE_PHONE_US_PLUS_ONE = /\b\+1\s*[\-(]?\d{3}\)?[\s.\-]+\d{3}[\s.\-]+\d{4}\b/g;

	/*
	 * PHONE_US_DOMESTIC: (AAA)BBB-CCCC or AAA-BBB-CCCC with separators . - space tolerated.
	 * Covers "312-555-0198" and "(415) 555-7821" shapes in fixtures.
	 * Flags: `g`; word boundary prefix avoids gluing digits to prior alnum.
	 */
	var RE_PHONE_US_DOMESTIC =
		/\b(?:\(\s*\d{3}\s*\)\s*[.\s\-]+|\d{3}\s*[.\-]\s*)\d{3}\s*[.\s\-]+\d{4}\b/g;

	/*
	 * PHONE_UK_LEADING_ZERO: Loose UK-ish pattern "(020)" style if near phone keywords — still gated.
	 * Risk: famous fake numbers — rely on DISALLOW + fixture prose ("famously fake", etc.).
	 */
	var RE_PHONE_UK_HINT = /\b\(0\d{2,4}\)\s*\d[\d\s\-]{6,}\b/g;

	/*
	 * ADDRESS_STREET: Number + directional token optional + alphanumeric street token + canonical suffix (see regex).
	 * Does not normalize international formats; ADDRESS_PO_BOX covers PO Boxes.
	 */
	var RE_ADDRESS_STREET =
		/\b\d{1,5}\s+[Nn]?[EW]?\s*[A-Za-z0-9][\w.]+\s+(?:Street|St|Avenue|Ave|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Terrace|Pkwy|Way|HWY|Hwy)[^\n\r]{0,160}/gi;

	/*
	 * ADDRESS_PO_BOX: USPS-style PO BOX ... trailing city / region-ish tail on same snippet.
	 */
	var RE_ADDRESS_PO_BOX =
		/\bP\.?\s*O\.?\s*Box\s+\d+[,\s]+[A-Za-z][^\n\r]{8,140}/gi;

	/*
	 * NAME_LEGAL: Label-led Legal name capture — case-insensitive prefix; trims value at parenthesis/dot.
	 * Coupled minimally with DISALLOW for nearby fiction cues on weak matches (Dear line separate).
	 */
	var RE_NAME_LEGAL = /\blegal name:\s*([^\n(]{2,120}?)(?:\s*\(|\.|$)/gi;

	/*
	 * NAME_PREFERRED: "preferred:" clause when it appears beside identity verification copy.
	 * Must still pass gatePreferredIdentityContext so random "preferred:" lines do not fire.
	 */
	var RE_NAME_PREFERRED_LINE =
		/\bpreferred:\s*([^\n(]{2,80}?)(?:\s*\(|\.|,|\n|$)/gi;

	/* Dear Firstname [...] comma/newline boundary — weakest signal; DISALLOW can veto. Flags: global. */
	var RE_NAME_DEAR =
		/\bDear\s+([A-Z][a-z]+(?:[\s.&][A-Za-z][a-z.]+)*(?:,?))(?=\s*[,\n])/g;

	/* -----------------------------------------------------------------------------
	 * Helpers
	 * -------------------------------------------------------------------------- */

	function sliceContext(body, start, end) {
		var b = Math.max(0, start - CONTEXT_CHARS_BEFORE);
		var e = Math.min(body.length, end + CONTEXT_CHARS_AFTER);
		return body.slice(b, e).toLowerCase();
	}

	function hotspotHasAny(lowerHotspot, phrases) {
		for (var i = 0; i < phrases.length; i++) {
			if (lowerHotspot.indexOf(phrases[i].toLowerCase()) !== -1) {
				return true;
			}
		}
		return false;
	}

	function hasDisallowedLoose(lowerHotspot) {
		return hotspotHasAny(lowerHotspot, DISALLOW_LOOSE_HIT);
	}

	function gateDobCandidate(body, matchStart, matchEnd) {
		var hot = sliceContext(body, matchStart, matchEnd);
		if (!hotspotHasAny(hot, KEYWORDS_DOB)) {
			return false;
		}
		if (hasDisallowedLoose(hot)) {
			return false;
		}
		if (hotspotHasAny(hot, DISALLOW_DOB_SPECIFIC)) {
			return false;
		}
		return true;
	}

	function gatePhoneCandidate(body, matchStart, matchEnd) {
		var hot = sliceContext(body, matchStart, matchEnd);
		if (!hotspotHasAny(hot, KEYWORDS_PHONE)) {
			return false;
		}
		if (hasDisallowedLoose(hot)) {
			return false;
		}
		return true;
	}

	function gateAddressCandidate(body, matchStart, matchEnd, hasExplicitPostalHint) {
		var hot = sliceContext(body, matchStart, matchEnd);
		if (!hotspotHasAny(hot, KEYWORDS_ADDRESS)) {
			return false;
		}
		if (hasDisallowedLoose(hot) && !hasExplicitPostalHint) {
			return false;
		}
		return true;
	}

	function gatePreferredIdentityContext(body, matchStart, matchEnd) {
		var hot = sliceContext(body, matchStart, matchEnd);
		return (
			hot.indexOf("preferred") !== -1 ||
			hot.indexOf("legal name") !== -1 ||
			hot.indexOf("verification") !== -1
		);
	}

	function normalizeMatch(re, body, type, gated) {
		var out = [];
		var m;
		re.lastIndex = 0;
		while ((m = re.exec(body)) !== null) {
			var s = m.index;
			var seg = m[0];
			var e = s + seg.length;
			if (typeof gated === "function" && !gated(body, s, e)) {
				continue;
			}
			out.push({
				start: s,
				end: e,
				info_type: type,
				info_value: seg,
			});
		}
		return out;
	}

	function capturesFromRegex(re, body, type, gate) {
		var rows = [];
		var m;
		re.lastIndex = 0;
		while ((m = re.exec(body)) !== null) {
			var g = m[1];
			if (g == null) {
				continue;
			}
			var idx = m.index + m[0].indexOf(g);
			var s = idx;
			var e = idx + g.length;
			if (typeof gate === "function" && !gate(body, s, e)) {
				continue;
			}
			rows.push({
				start: s,
				end: e,
				info_type: type,
				info_value: g.trim(),
			});
		}
		return rows;
	}

	function mergeNonOverlapping(bodyLength, extractions) {
		var list = extractions
			.map(function (row) {
				return {
					start: Math.max(0, Math.floor(Number(row.start))),
					end: Math.max(0, Math.floor(Number(row.end))),
					info_type: row.info_type,
					info_value: row.info_value,
				};
			})
			.filter(function (row) {
				return row.end > row.start && row.end <= bodyLength;
			})
			.sort(function (a, b) {
				if (a.start !== b.start) return a.start - b.start;
				return b.end - a.end;
			});

		var safe = [];
		for (var i = 0; i < list.length; i++) {
			if (safe.length && list[i].start < safe[safe.length - 1].end) {
				continue;
			}
			safe.push(list[i]);
		}
		return safe;
	}

	function extendStreetToCityZipIfPresent(body, row) {
		var windowEnd = Math.min(body.length, row.end + 200);
		var zone = body.slice(row.start, windowEnd);
		var mz = zone.match(/\b[A-Za-z][^\n]{3,}?,\s*[A-Za-z][A-Za-z \-]{2,24}\s+[A-Za-z\d]{3,14}\b/) || zone.match(/\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b/);
		if (!mz || mz.index == null || mz[0].length === 0) {
			return row;
		}
		var delta = mz.index + mz[0].length;
		row.end = Math.min(body.length, row.start + delta);
		row.info_value = body.slice(row.start, row.end).replace(/\s+/g, " ").trim();
		return row;
	}

	function scanStreetAddresses(body, acc) {
		RE_ADDRESS_STREET.lastIndex = 0;
		var m;
		while ((m = RE_ADDRESS_STREET.exec(body)) !== null) {
			var s = m.index;
			var e = m.index + m[0].length;
			var hinted = /\d{5}(?:-\d{4})?/.test(m[0]);
			var row = extendStreetToCityZipIfPresent(body, {
				start: s,
				end: e,
				info_type: TYPE_ADDRESS,
				info_value: m[0],
			});
			var hot = sliceContext(body, row.start, row.end);
			hinted = hinted || /\d{5}(?:-\d{4})?/.test(row.info_value) || /[A-Z]\d[A-Z]\s?\d[A-Z]\d/i.test(row.info_value);
			if (!gateAddressCandidate(body, row.start, row.end, hinted)) {
				continue;
			}
			if (hasDisallowedLoose(hot) && !hinted && hot.indexOf("delivery") === -1 && hot.indexOf("address") === -1) {
				continue;
			}
			acc.push(row);
		}
		return acc;
	}

	function scanPoBoxes(body, acc) {
		RE_ADDRESS_PO_BOX.lastIndex = 0;
		var m;
		while ((m = RE_ADDRESS_PO_BOX.exec(body)) !== null) {
			var s = m.index;
			var e = m.index + m[0].length;
			var row = { start: s, end: e, info_type: TYPE_ADDRESS, info_value: m[0].replace(/\s+/g, " ").trim() };
			if (!gateAddressCandidate(body, row.start, row.end, /\d/.test(row.info_value.slice(-6)))) {
				continue;
			}
			acc.push(row);
		}
		return acc;
	}

	function scan(body) {
		if (typeof body !== "string" || !body.length) {
			return [];
		}

		var acc = [];

		acc = acc.concat(
			normalizeMatch(RE_DOB_ISO, body, TYPE_DOB, gateDobCandidate),
			normalizeMatch(RE_DOB_MDY_SLASH_OR_DASH, body, TYPE_DOB, gateDobCandidate),
			normalizeMatch(RE_DOB_DMY_DOT, body, TYPE_DOB, gateDobCandidate),
			normalizeMatch(RE_DOB_MONTH_NAME, body, TYPE_DOB, gateDobCandidate),

			normalizeMatch(RE_PHONE_US_PLUS_ONE, body, TYPE_PHONE, gatePhoneCandidate),
			normalizeMatch(RE_PHONE_US_DOMESTIC, body, TYPE_PHONE, gatePhoneCandidate),
			normalizeMatch(RE_PHONE_UK_HINT, body, TYPE_PHONE, gatePhoneCandidate)
		);

		scanStreetAddresses(body, acc);
		scanPoBoxes(body, acc);

		acc = acc.concat(
			capturesFromRegex(RE_NAME_LEGAL, body, TYPE_NAME, function always() {
				return true;
			}),
			capturesFromRegex(RE_NAME_PREFERRED_LINE, body, TYPE_NAME, function (b, start, vend) {
				return gatePreferredIdentityContext(b, start, vend);
			}),
			capturesFromRegex(RE_NAME_DEAR, body, TYPE_NAME, function (b, s, e) {
				var hot = sliceContext(b, s, e);
				return !hasDisallowedLoose(hot);
			})
		);

		return mergeNonOverlapping(body.length, acc);
	}

	global.EmailPiiLabDeterministic = { scan: scan };
})(typeof window !== "undefined" ? window : typeof global !== "undefined" ? global : this);
