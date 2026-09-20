(function () {
  const PluginApi = window.PluginApi;
  if (!PluginApi || !PluginApi.React || !PluginApi.patch) {
    return;
  }

  const React = PluginApi.React;
  const Bootstrap = PluginApi.libraries && PluginApi.libraries.Bootstrap;
  const Button = Bootstrap && Bootstrap.Button;
  const Alert = Bootstrap && Bootstrap.Alert;
  const REVIEW_TAG = "Multi-Booru Review";
  const PLUGIN_ID = "DanbooruTagImporter";
  const REVIEW_CONFIDENCE_KEY = "booru-importer-review-confidence";

  function isSupportedBooruUrl(url) {
    const text = String(url || "").trim();
    return (
      /danbooru\.donmai\.us\/posts\/\d+/i.test(text) ||
      /gelbooru\.com\/.*[?&]id=\d+/i.test(text) ||
      /rule34\.xxx\/.*[?&]id=\d+/i.test(text) ||
      /e621\.net\/posts\/\d+/i.test(text)
    );
  }

  function parseReviewCandidate(url) {
    const storedUrl = String(url || "").trim();
    let displayUrl = storedUrl;
    let confidence = null;
    try {
      const parsed = new URL(storedUrl, window.location.origin);
      const fragment = new URLSearchParams(parsed.hash.replace(/^#/, ""));
      const raw = fragment.get(REVIEW_CONFIDENCE_KEY);
      if (raw !== null && raw !== "") {
        const score = Number(raw);
        if (Number.isFinite(score)) {
          confidence = Math.max(0, Math.min(100, score));
        }
        parsed.hash = "";
        displayUrl = parsed.toString();
      }
    } catch (_) {
      // Keep the original URL if parsing fails; the backend still validates it.
    }
    return { storedUrl, displayUrl, confidence };
  }

  async function submitDecision(imageId, candidateUrl, action) {
    const query = `
      mutation BooruImporterReviewDecision($pluginId: ID!, $args: Map) {
        runPluginOperation(plugin_id: $pluginId, args: $args)
      }
    `;

    const baseHref = document.querySelector("base")?.getAttribute("href") || "/";
    const platformBase = new URL(baseHref, window.location.origin);
    const graphqlUrl = new URL("graphql", platformBase).toString();

    const response = await fetch(graphqlUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        query,
        variables: {
          pluginId: PLUGIN_ID,
          args: {
            mode: "review_decision",
            action,
            image_id: String(imageId),
            candidate_url: candidateUrl,
          },
        },
      }),
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(`Stash returned HTTP ${response.status}`);
    }
    if (payload.errors && payload.errors.length) {
      throw new Error(payload.errors.map((entry) => entry.message).join("; "));
    }
    return payload.data && payload.data.runPluginOperation;
  }

  function ReviewDecisionPanel(props) {
    const image = props.image;
    const candidateUrl = props.candidateUrl;
    const displayUrl = props.displayUrl;
    const confidence = props.confidence;
    const [busy, setBusy] = React.useState("");
    const [error, setError] = React.useState("");

    async function choose(action) {
      if (busy) return;
      setBusy(action);
      setError("");
      try {
        await submitDecision(image.id, candidateUrl, action);
        window.location.reload();
      } catch (err) {
        setError(err && err.message ? err.message : String(err));
        setBusy("");
      }
    }

    const yesLabel = busy === "yes" ? "Importing..." : "Yes — import this source";
    const noLabel = busy === "no" ? "Saving..." : "No — mark No Match";

    const buttons = Button
      ? React.createElement(
          React.Fragment,
          null,
          React.createElement(
            Button,
            {
              variant: "success",
              size: "sm",
              disabled: Boolean(busy),
              onClick: function () { choose("yes"); },
              className: "mr-2",
            },
            yesLabel
          ),
          React.createElement(
            Button,
            {
              variant: "danger",
              size: "sm",
              disabled: Boolean(busy),
              onClick: function () { choose("no"); },
            },
            noLabel
          )
        )
      : React.createElement(
          React.Fragment,
          null,
          React.createElement(
            "button",
            {
              type: "button",
              disabled: Boolean(busy),
              onClick: function () { choose("yes"); },
              className: "btn btn-success btn-sm mr-2",
            },
            yesLabel
          ),
          React.createElement(
            "button",
            {
              type: "button",
              disabled: Boolean(busy),
              onClick: function () { choose("no"); },
              className: "btn btn-danger btn-sm",
            },
            noLabel
          )
        );

    const errorNode = error
      ? (Alert
          ? React.createElement(Alert, { variant: "danger", className: "mt-2 mb-0" }, error)
          : React.createElement("div", { className: "alert alert-danger mt-2 mb-0" }, error))
      : null;

    return React.createElement(
      "div",
      { className: "card bg-secondary text-white mt-3 mb-3" },
      React.createElement(
        "div",
        { className: "card-body" },
        React.createElement("h5", { className: "card-title" }, "Booru Importer Review"),
        React.createElement(
          "p",
          { className: "mb-2" },
          "Is this source link the correct match for this image?"
        ),
        React.createElement(
          "p",
          { className: "mb-2" },
          React.createElement("strong", null, "Review confidence: "),
          confidence === null
            ? "Not recorded — recheck this Review candidate to populate it."
            : confidence.toFixed(1) + "% SauceNAO similarity (Review band: 85.0–92.9%)"
        ),
        React.createElement(
          "p",
          { className: "mb-3 text-break" },
          React.createElement(
            "a",
            {
              href: displayUrl,
              target: "_blank",
              rel: "noopener noreferrer",
            },
            displayUrl
          )
        ),
        buttons,
        errorNode
      )
    );
  }

  PluginApi.patch.after("ImageDetailPanel", function (props, _context, rendered) {
    const image = props && props.image;
    if (!image) {
      return rendered;
    }

    const hasReview = (image.tags || []).some(function (tag) {
      return String((tag && tag.name) || "").toLowerCase() === REVIEW_TAG.toLowerCase();
    });
    if (!hasReview) {
      return rendered;
    }

    const supportedUrls = (image.urls || []).filter(isSupportedBooruUrl);
    if (!supportedUrls.length) {
      return rendered;
    }

    const candidate = parseReviewCandidate(supportedUrls[supportedUrls.length - 1]);
    const candidateUrl = candidate.storedUrl;
    return React.createElement(
      React.Fragment,
      null,
      rendered,
      React.createElement(ReviewDecisionPanel, {
        image: image,
        candidateUrl: candidateUrl,
        displayUrl: candidate.displayUrl,
        confidence: candidate.confidence,
      })
    );
  });
})();
