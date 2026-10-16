// Timeline ticks sit on local clock hours divisible by the step for the
// strip's length: three or four ticks whatever the window.
const TICK_HOURS = { 6: 2, 12: 3, 24: 6, 48: 12 };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

// The host locale as Intl accepts it. Host runtimes may expose POSIX locale
// names such as "C", which Intl rejects.
function intlLocale(locale) {
  try {
    return Intl.getCanonicalLocales(locale)[0] ?? "en";
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
    return "en";
  }
}

// A time on the fetch day is shown as a clock time; an older one also gets its
// date. The fetch time, not the browser clock, is the reference, so a frame
// rasterised later still says what the data said.
function timeText(ms, reference, locale) {
  const moment = new Date(ms);
  const sameDay = moment.toDateString() === new Date(reference).toDateString();
  return new Intl.DateTimeFormat(locale, {
    ...(sameDay ? {} : { day: "numeric", month: "short" }),
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(moment);
}

function noteMarkup(message, detail = "") {
  return `
    <div class="w-body">
      <p class="u-muted">${escapeHtml(message)}</p>
      ${detail ? `<small class="u-muted">${escapeHtml(detail)}</small>` : ""}
    </div>`;
}

// One status tile: the icon is what reads from across the room, the text
// under or beside it names the state and its time.
function tileMarkup(kind, icon, label, detail, span = 1) {
  return `
    <div class="tile is-${kind}"${span > 1 ? ` style="grid-column: span ${span}"` : ""}>
      <div class="tile-body" style="--label-chars: ${label.length}; --detail-chars: ${detail.length || 1}">
        <i class="tile-icon ph-bold ph-${escapeHtml(icon)}" aria-hidden="true"></i>
        <div class="tile-copy">
          <div class="tile-label">${escapeHtml(label)}</div>
          ${detail ? `<div class="tile-detail">${escapeHtml(detail)}</div>` : ""}
        </div>
      </div>
    </div>`;
}

function statusMarkup(data, ctx, locale) {
  const time = (ms) => timeText(ms, data.timeline_end, locale);
  const iconsOnly = ctx.cell.options?.icons_only === true ? " is-icons" : "";
  if (!data.alerts.length) {
    return `<div class="tiles${iconsOnly}">${tileMarkup("clear", "shield-check", ctx.t("status.clear", "No alerts"), "")}</div>`;
  }

  // Up to three alerts sit in one row; more wrap into two rows. At xs two
  // alerts stack instead: wide, short tiles keep their text beside the icon
  // and so larger than in two narrow columns.
  const count = data.alerts.length;
  const columns = ctx.cell.size === "xs"
    ? (count <= 2 ? 1 : 2)
    : (count <= 3 ? count : Math.ceil(count / 2));
  // A short last row would leave an empty slot; its last tile takes the rest.
  const lastRow = count % columns;
  const tiles = data.alerts.map((alert, index) => tileMarkup(
    "alert",
    alert.icon,
    ctx.t(alert.label_key, alert.label),
    alert.since === null ? "" : ctx.t("status.since", "since {time}").replace("{time}", time(alert.since)),
    lastRow && index === count - 1 ? columns - lastRow + 1 : 1,
  ));
  return `<div class="tiles${iconsOnly}" style="--columns: ${columns}">${tiles.join("")}</div>`;
}

// The last timeline_hours as a strip with the alert spans filled in. Positions are
// percentages of the window, so the strip needs no measuring.
function timelineMarkup(data, locale) {
  const start = data.timeline_start;
  const span = data.timeline_end - start;
  const percent = (ms) => ((ms - start) / span) * 100;
  const bars = data.timeline.map(([from, to]) => (
    `<span class="timeline-span" style="left: ${percent(from)}%; width: ${percent(to) - percent(from)}%"></span>`
  )).join("");

  // setHours() works in local time, so ticks stay on clock hours across DST.
  const hourFormat = new Intl.DateTimeFormat(locale, { hour: "2-digit", hourCycle: "h23" });
  const step = TICK_HOURS[data.timeline_hours];
  const tick = new Date(start);
  tick.setHours(tick.getHours() - (tick.getHours() % step) + step, 0, 0, 0);
  const ticks = [];
  while (tick.getTime() < data.timeline_end) {
    ticks.push(`<span class="timeline-tick" style="left: ${percent(tick.getTime())}%">${escapeHtml(hourFormat.format(tick))}</span>`);
    tick.setHours(tick.getHours() + step);
  }
  return `
    <div class="timeline" aria-label="${data.timeline_hours} h">
      <div class="timeline-bar">${bars}</div>
      <div class="timeline-ticks">${ticks.join("")}</div>
    </div>`;
}

const styles = `
  <link rel="stylesheet" href="/static/style/spectra-widgets.css" />
  <style>
    .w.ukraine-alarm {
      display: grid;
      /* Region names are long; the column must not grow to the unwrapped title. */
      grid-template-columns: minmax(0, 1fr);
      /* The footer row exists only when there is a footer: an empty track
         would still take a gap under the tiles. */
      grid-template-rows: auto minmax(0, 1fr);
      grid-auto-rows: auto;
      gap: clamp(var(--space-1), 2cqmin, var(--space-3));
      padding: clamp(var(--space-2), 3cqmin, var(--space-4));
    }
    .w-body p { margin: 0; }
    .tiles {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-columns: repeat(var(--columns, 1), minmax(0, 1fr));
      grid-auto-rows: minmax(0, 1fr);
      gap: clamp(var(--space-1), 2cqmin, var(--space-3));
    }
    /* Each tile is its own size container, so the icon and text scale with
       the tile, whatever the cell size and the number of alerts. Layout rules
       live on .tile-body: a container cannot query itself. */
    .tile {
      container-type: size;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
    }
    /* An active alert inverts the tile: the strongest contrast every theme,
       including the 1-bit one, can draw. The foreground is --on-accent, not
       --surface: canvas hosts reset --surface (and --bg) to transparent so the
       board shows through, which would make the icon and text invisible. */
    .tile.is-alert {
      background: var(--text-primary);
      color: var(--on-accent);
    }
    .tile.is-clear { background: var(--surface-sunken); }
    .tile-body {
      height: 100%;
      display: grid;
      align-content: center;
      justify-items: center;
      gap: 4cqh;
      padding: 5cqmin;
      text-align: center;
    }
    .tile-icon {
      font-size: min(46cqh, 60cqw);
      line-height: 1;
    }
    .tile-copy { min-width: 0; }
    .tile-label {
      font-size: min(11cqh, 9cqw);
      font-weight: var(--fw-black);
      line-height: 1.05;
      overflow-wrap: break-word;
      text-wrap: balance;
    }
    .tile-detail {
      margin-top: 2cqh;
      font-size: min(7.5cqh, 6cqw);
      font-weight: var(--fw-bold);
      font-variant-numeric: tabular-nums;
    }
    .tile.is-clear .tile-detail { color: var(--text-secondary); }
    /* A wide, short tile puts the icon beside the text instead of above it. */
    /* Its text never wraps: a wrapped line's box fills the whole column and
       pulls the icon-and-text group off centre. Instead the font shrinks to
       fit the line, from the room left beside the icon (100cqw less the icon,
       gap and padding) and the character count; .68em is a safe average
       advance for the bold Latin and Cyrillic faces. */
    @container (min-aspect-ratio: 2 / 1) {
      .tile-body {
        grid-template-columns: auto max-content;
        justify-content: center;
        align-items: center;
        gap: 6cqh;
        white-space: nowrap;
      }
      .tile-icon { font-size: 66cqh; }
      .tile-label { font-size: min(18cqh, calc((100cqw - 86cqh) / (.68 * var(--label-chars)))); }
      .tile-detail { font-size: min(11cqh, calc((100cqw - 86cqh) / (.68 * var(--detail-chars)))); }
    }
    /* A small or narrow tile drops the time, a tiny one keeps the icon alone:
       text scaled down that far is no longer readable. */
    @container (max-height: 80px) or (max-width: 150px) {
      .tile-detail { display: none; }
    }
    @container (max-width: 110px) and (max-height: 90px) {
      .tile-copy { display: none; }
      .tile-icon { font-size: 70cqmin; }
    }
    .tiles.is-icons .tile-copy { display: none; }
    .tiles.is-icons .tile-body { grid-template-columns: auto; }
    .tiles.is-icons .tile-icon { font-size: 70cqmin; }
    .history-error {
      color: var(--text-muted);
      font-size: var(--fs-caption);
      font-weight: var(--fw-semi);
    }
    /* The outline is what shows an empty day: the 1-bit theme draws
       --surface-sunken as plain white. */
    .timeline-bar {
      position: relative;
      height: clamp(.6em, 5cqmin, 1.4em);
      background: var(--surface-sunken);
      border: 2px solid var(--text-primary);
    }
    .timeline-span {
      position: absolute;
      top: 0;
      bottom: 0;
      background: var(--text-primary);
    }
    .timeline-ticks {
      position: relative;
      height: 1.5em;
      color: var(--text-secondary);
      font-size: var(--fs-caption);
      font-weight: var(--fw-bold);
      font-variant-numeric: tabular-nums;
    }
    .timeline-tick {
      position: absolute;
      top: .5em;
      transform: translateX(-50%);
    }
    /* A mark under the strip pins each hour label to its point in time. */
    .timeline-tick::before {
      content: "";
      position: absolute;
      top: -.5em;
      left: calc(50% - 1px);
      width: 2px;
      height: .35em;
      background: currentColor;
    }

    /* The strip would leave the tiles no room at xs. */
    .ukraine-alarm[data-size="xs"] .timeline { display: none; }
  </style>`;

export default function render(shadow, ctx) {
  // server.py owns every normalization and fallback, so its payload is rendered
  // as received. When that call raised or timed out the host substitutes a bare
  // {error}, or null if it produced nothing; both read as a failed load.
  const data = ctx.data ?? { error_key: "error.fetch" };
  // CSS zoom scales the host's zoom-locked title bar as one unit: icon, text,
  // min-height and rule. A blank or invalid value leaves it at 1.
  const titleScale = Number(ctx.cell.options?.title_scale) || 1;
  const mount = (icon, title, body) => {
    shadow.innerHTML = `${styles}
    <div class="w ukraine-alarm" data-widget="ha_ukraine_alarm" data-size="${ctx.cell.size}">
      <div class="w-title" style="zoom: ${titleScale}">
        <i class="ph-bold ${icon}" aria-hidden="true"></i>
        <h3>${escapeHtml(title)}</h3>
      </div>
      ${body}
    </div>`;
  };
  const cardTitle = data.title || ctx.t("title.default", "Ukraine Alarm");

  if (data.setup) {
    mount("ph-siren", cardTitle, noteMarkup(ctx.t("setup.select_region", "Select a Ukraine Alarm region.")));
    return;
  }
  if (data.error_key || data.error) {
    const key = data.error_key ?? "error.fetch";
    const fallback = {
      "error.missing_core": "Install and configure Home Assistant Core for this widget.",
      "error.no_region": "No Ukraine Alarm data for this region. Check the ukraine_alarm integration in Home Assistant.",
    }[key] ?? "Could not load current Home Assistant states. Check the Home Assistant Core settings.";
    mount("ph-warning-circle", cardTitle, noteMarkup(ctx.t(key, fallback), data.error));
    return;
  }

  const locale = intlLocale(ctx.locale);
  // A failed history read leaves the spans incomplete, so the strip gives way
  // to the reason instead of drawing a misleadingly quiet day. The reason
  // stays without a strip too: it is why the alerts have no times.
  let footer = "";
  if (data.history_error) {
    footer = `<div class="history-error">${escapeHtml(ctx.t("status.history_error", "Could not load alert history"))}</div>`;
  } else if (data.timeline_hours) {
    footer = timelineMarkup(data, locale);
  }
  mount("ph-siren", data.title, `
      ${statusMarkup(data, ctx, locale)}
      ${footer}`);
}
