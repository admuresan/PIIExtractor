/**
 * Read genai/toolkits/architectural_instructions before making changes.
 * Mirrors requirements_matcher/static/js/assistantChat/formatDetails.js for token UI.
 */

(function attachEmailPiiLabFormat(root) {
	/** @type {typeof window} */
	const g = typeof root !== "undefined" ? root : /** @type {typeof window} */ ({});

	function formatAssistantDuration(ms, lookup) {
		const n = Number(ms);
		if (!Number.isFinite(n) || n < 0) return "";
		const sec = Math.max(0, Math.round(n / 1000));
		if (sec < 60) {
			if (sec === 1) return `1 ${lookup("assistant_chat_second", "second")}`;
			return `${sec} ${lookup("assistant_chat_seconds", "seconds")}`;
		}
		const m = Math.floor(sec / 60);
		const s = sec % 60;
		return `${m}:${String(s).padStart(2, "0")}`;
	}

	function formatTokenCountSuffix(n) {
		const x = Number(n);
		if (!Number.isFinite(x)) return String(n);
		if (x === 0) return "0";
		const sign = x < 0 ? "-" : "";
		const abs = Math.abs(x);
		if (abs < 1000) {
			return sign + String(Math.round(abs));
		}
		const tiers = [
			[1e9, "b"],
			[1e6, "m"],
			[1e3, "k"],
		];
		for (const [th, suf] of tiers) {
			if (abs >= th) {
				const val = abs / th;
				const rounded = Number(val.toPrecision(3));
				let str;
				if (rounded >= 100) str = rounded.toFixed(0);
				else if (rounded >= 10) str = rounded.toFixed(1).replace(/\.0$/, "");
				else
					str = rounded
						.toFixed(2)
						.replace(/(\.\d*?)0+$/, "$1")
						.replace(/\.$/, "");
				return sign + str + suf;
			}
		}
		return sign + String(Math.round(abs));
	}

	function stringifyTokenDetails(obj) {
		if (obj == null) return JSON.stringify(obj, null, 2);
		let clone;
		try {
			clone = JSON.parse(JSON.stringify(obj));
		} catch {
			return JSON.stringify(obj, null, 2);
		}
		function walk(o) {
			if (o == null || typeof o !== "object") return;
			if (Array.isArray(o)) {
				for (const item of o) walk(item);
				return;
			}
			for (const k of Object.keys(o)) {
				const v = /** @type {Record<string, unknown>} */ (o)[k];
				if (typeof v === "number" && Number.isFinite(v) && /token/i.test(k)) {
					/** @type {Record<string, unknown>} */ (o)[k] =
						formatTokenCountSuffix(v);
				} else if (v != null && typeof v === "object") {
					walk(v);
				}
			}
		}
		walk(clone);
		return JSON.stringify(clone, null, 2);
	}

	g.EmailPiiLabFormat = {
		formatAssistantDuration,
		formatTokenCountSuffix,
		stringifyTokenDetails,
	};
})(typeof window !== "undefined" ? window : globalThis);
