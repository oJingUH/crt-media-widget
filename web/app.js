/* ============================================================
   CRT-MEDIA // app.js
   Standalone phosphor widget.  Live bridge = window.pywebview.api
   (provided by app.py).  With no bridge, or with ?demo=1, the page
   renders entirely from a built-in DEMO_STATE and reports LINK:DEMO.
   ============================================================ */
(function () {
  'use strict';

  /* Query flags may arrive in the search string (?demo=1) or, when app.py or the
     harness loads the page from a file:// URL, in the fragment (#demo=1) —
     WebView2 percent-encodes a '?' in a file URL and breaks the load, so the
     fragment is the safe channel there. */
  function flag(name) {
    var s = new URLSearchParams(location.search || '');
    if (s.has(name)) return s.get(name);
    var h = new URLSearchParams((location.hash || '').replace(/^#/, ''));
    return h.has(name) ? h.get(name) : null;
  }

  var QS = { get: flag };
  var POLL_MS = 600;
  var CANVAS_W = 360, CANVAS_H = 400;
  var FORCE_DEMO = QS.get('demo') === '1';
  /* The metrics object stays available on the live widget so the shell and the
     probes can read window.__SELFTEST__ as the objective health check; pass
     selftest=0 to switch the periodic tick off. */
  var SELFTEST_ON = QS.get('selftest') !== '0';
  var FORCED_STATE = (QS.get('state') || '').toLowerCase();   // 'paused' from the harness
  var ART_GRID = 40;                                          // coarse grid -> 160/40 = 4px blocks
  var BOOT_TYPE_MS = 1250, BOOT_HOLD_MS = 350;                // ~1.6 s total

  /* ---------------------------------------------------------- refs */
  function $(id) { return document.getElementById(id); }
  var D = {
    bezel: $('bezel'), screen: $('screen'), content: $('content'),
    hdrTxt: $('hdrTxt'), hdrVer: $('hdrVer'),
    statusText: $('statusText'), cursor: $('cursor'),
    artframe: $('artframe'), artcv: $('artcv'), artph: $('artph'), arttag: $('arttag'),
    mTitle: $('mTitle'), mArtist: $('mArtist'), mAlbum: $('mAlbum'), mStatus: $('mStatus'),
    tElapsed: $('tElapsed'), tTotal: $('tTotal'), pbar: $('pbar'), pfill: $('pfill'), phead: $('phead'),
    transport: $('transport'), btnPrev: $('btnPrev'), btnPlay: $('btnPlay'), btnNext: $('btnNext'),
    vol: $('vol'), volLab: $('volLab'), vbar: $('vbar'), vfill: $('vfill'), volPct: $('volPct'),
    sessions: $('sessions'), chips: $('chips'), btnMute: $('btnMute'), muteTxt: $('muteTxt'),
    boot: $('boot'), bootLines: $('bootLines'), btnMin: $('btnMin'), btnClose: $('btnClose'), grip: $('grip'),
    viz: $('viz')
  };

  /* ---------------------------------------------------------- bridge */
  function bridge() {
    try {
      if (window.pywebview && window.pywebview.api) return window.pywebview.api;
    } catch (e) { /* ignore */ }
    return null;
  }
  function isDemo() { return FORCE_DEMO || !bridge(); }

  function call(name, args) {
    var b = bridge();
    if (!b || typeof b[name] !== 'function') return Promise.resolve(null);
    try { return Promise.resolve(b[name].apply(b, args || [])); }
    catch (e) { return Promise.resolve(null); }
  }

  /* ---------------------------------------------------------- demo state */
  var DEMO_SESSIONS = [
    { app_id: 'SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify', app_name: 'Spotify',
      title: 'NEON CORRIDOR (EXTENDED MIX)', artist: 'PHOSPHOR CHOIR', status: 'playing' },
    { app_id: 'Chrome', app_name: 'Chrome',
      title: 'Lofi Beats To Debug To', artist: 'youtube.com', status: 'paused' },
    { app_id: 'Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic', app_name: 'Media Player',
      title: 'Tape Loop 07', artist: 'UNKNOWN', status: 'paused' }
  ];
  var DEMO_TITLE = 'NEON CORRIDOR (EXTENDED MIX)';
  var DEMO_ARTIST = 'PHOSPHOR CHOIR';
  var DEMO_ALBUM = 'P1 ARCHIVE VOL. 2';
  var DEMO_VOLUME = 0.62;
  /* demo-only test knobs, so the harness can exercise the no-artwork and
     null-timeline renderings that the default demo path never reaches */
  var DEMO_NO_ART = flag('art') === '0';
  var DEMO_NA = flag('na') === '1';
  var demo = {
    t0: (window.performance && performance.now) ? performance.now() : Date.now(),
    base: 212.036,
    duration: 297.892,
    paused: FORCED_STATE === 'paused',
    volume: DEMO_VOLUME,
    muted: false,
    activeApp: DEMO_SESSIONS[0].app_id,
    artKey: 'demo-art'
  };

  function nowMs() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

  function demoPosition() {
    if (demo.paused) return demo.base % demo.duration;
    var p = demo.base + (nowMs() - demo.t0) / 1000;
    return p % demo.duration;
  }

  function demoState() {
    var pos = demoPosition();
    return {
      ok: true, has_session: true,
      app_id: demo.activeApp,
      app_name: (DEMO_SESSIONS.filter(function (s) { return s.app_id === demo.activeApp; })[0] || DEMO_SESSIONS[0]).app_name,
      title: DEMO_TITLE,
      artist: DEMO_ARTIST,
      album: DEMO_ALBUM,
      status: demo.paused ? 'paused' : 'playing',
      position: DEMO_NA ? null : pos,
      duration: DEMO_NA ? null : demo.duration,
      can_seek: DEMO_NA ? false : true, can_next: true, can_previous: true,
      art_key: DEMO_NO_ART ? null : demo.artKey,
      volume: demo.volume, muted: demo.muted,
      sessions: DEMO_SESSIONS.map(function (s) {
        return { app_id: s.app_id, app_name: s.app_name, title: s.title, artist: s.artist, status: s.status };
      }),
      t_ms: nowMs(),
      error: null
    };
  }

  /* The honest empty widget: what renders whenever there is no live sample and
     we are NOT in demo mode.  Dashes, placeholder art, unavailable bar - never
     fabricated track data. */
  var EMPTY_STATE = {
    ok: false, has_session: false,
    app_id: null, app_name: null, title: null, artist: null, album: null,
    status: 'unknown', position: null, duration: null,
    can_seek: false, can_next: false, can_previous: false,
    art_key: null, volume: null, muted: false, sessions: [],
    t_ms: 0, error: null
  };

  /* ---------------------------------------------------------- state */
  var S = null;              // last applied state
  var recvPerf = 0;          // our clock at the moment the sample was taken
  var lastTms = null;        // bridge monotonic ms of the last accepted sample
  var link = 'WAIT';         // OK | DEMO | ERR | WAIT
  var seekPreview = null;    // seconds while dragging the bar

  function apply(st, t0, t1) {
    if (!st || typeof st !== 'object') return;
    S = st;
    if (st.t_ms !== lastTms) {          // a genuinely new sample: re-anchor the extrapolation
      lastTms = st.t_ms;
      recvPerf = (typeof t0 === 'number' && typeof t1 === 'number') ? (t0 + t1) / 2 : nowMs();
    }
    link = isDemo() ? 'DEMO' : (st.ok === false ? 'ERR' : 'OK');
    render();
  }

  /* ---------------------------------------------------------- formatting */
  function fmtTime(s) {
    if (s === null || s === undefined || !isFinite(s) || s < 0) return '--:--';
    s = Math.floor(s);
    var m = Math.floor(s / 60), ss = s % 60;
    return (m < 10 ? '0' + m : '' + m) + ':' + (ss < 10 ? '0' + ss : '' + ss);
  }
  function pct(x) { return Math.round(Math.max(0, Math.min(1, x)) * 100); }
  function clamp01(x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }

  function positionNow() {
    if (seekPreview !== null) return seekPreview;
    if (!S) return null;
    if (S.position === null || S.position === undefined) return null;
    if (S.status === 'playing') {
      // t_ms lives on Python's time.monotonic() clock and shares no epoch with
      // performance.now(), so extrapolate from our own receive timestamp; t_ms is
      // used only to detect that a fresh sample arrived.
      var d = (nowMs() - recvPerf) / 1000;
      var p = S.position + (d > 0 ? d : 0);
      if (S.duration !== null && S.duration !== undefined && S.duration > 0) p = Math.min(p, S.duration);
      return p;
    }
    return S.position;
  }

  /* ---------------------------------------------------------- render */
  function render() {
    /* Demo content may only ever render in actual demo mode.  In every other
       case a missing state renders the honest empty widget, so the ERR / WAIT
       link states never present fabricated track data as the user's media. */
    var s = S || (isDemo() ? demoState() : EMPTY_STATE);
    var appName = (s.app_name || '').toUpperCase();
    var demoNow = isDemo();

    /* 10. status readout line */
    var volTxt = s.muted ? 'MUT' : (pct(s.volume) + '%');
    D.statusText.textContent =
      'SRC:' + (appName ? appName.slice(0, 11) : '---') +
      '  VOL:' + (s.volume === null || s.volume === undefined ? '--' : volTxt) +
      '  LINK:' + link +
      '  CLK:' + clockText();

    /* meta */
    setMarquee(D.mTitle, s.title || '--');
    setMarquee(D.mArtist, s.artist || '--');
    setMarquee(D.mAlbum, s.album || '--');
    setMarquee(D.mStatus, (s.status || 'unknown').toUpperCase());

    /* 9. progress */
    var pos = positionNow();
    var dur = (s.duration === null || s.duration === undefined) ? null : s.duration;
    var usable = (pos !== null && dur !== null && dur > 0);
    D.pbar.classList.toggle('na', !usable);
    if (usable) {
      var f = clamp01(pos / dur);
      D.pfill.style.width = (f * 100).toFixed(2) + '%';
      D.phead.style.left = (f * 100).toFixed(2) + '%';
      D.pbar.setAttribute('aria-valuenow', String(Math.round(f * 100)));
      D.pbar.setAttribute('aria-valuetext', fmtTime(pos) + ' of ' + fmtTime(dur));
    } else {
      D.pfill.style.width = '0%';
      D.phead.style.left = '0%';
      D.pbar.setAttribute('aria-valuenow', '0');
      D.pbar.setAttribute('aria-valuetext', 'unavailable');
    }
    D.tElapsed.textContent = fmtTime(pos);
    D.tTotal.textContent = fmtTime(dur);

    /* transport */
    var playing = s.status === 'playing';
    D.btnPlay.classList.toggle('is-paused', playing);
    D.btnPlay.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    D.btnPrev.setAttribute('aria-disabled', s.can_previous === false ? 'true' : 'false');
    D.btnNext.setAttribute('aria-disabled', s.can_next === false ? 'true' : 'false');

    /* volume */
    var vol = (s.volume === null || s.volume === undefined) ? 0 : clamp01(s.volume);
    D.vfill.style.width = (vol * 100).toFixed(1) + '%';
    D.vbar.setAttribute('aria-valuenow', String(pct(vol)));
    D.vbar.setAttribute('aria-valuetext', pct(vol) + ' percent' + (s.muted ? ', muted' : ''));
    D.vol.classList.toggle('muted', !!s.muted);
    D.volLab.textContent = s.muted ? 'MUT' : 'VOL';
    D.volPct.textContent = s.muted ? 'MUTE'
      : ((s.volume === null || s.volume === undefined) ? '--%' : (pct(vol) + '%'));
    D.btnMute.classList.toggle('on', !!s.muted);
    D.muteTxt.textContent = s.muted ? 'UNMUTE' : 'MUTE';
    D.btnMute.setAttribute('aria-label', s.muted ? 'Unmute system volume' : 'Mute system volume');

    renderSessions(s);
    ensureArt(s.art_key, s.app_id, s.title);
  }

  var renderedSessions = '';
  var pinnedAppId = '';      // '' = follow the system's current session (auto)
  function renderSessions(s) {
    var list = (s && s.sessions) || [];
    var key = list.map(function (x) { return x.app_id + '|' + x.status; }).join('#') +
      '|' + s.app_id + '|' + pinnedAppId;
    if (key === renderedSessions) return;
    renderedSessions = key;
    D.chips.textContent = '';

    /* leading chip: the visible way back to auto-follow (select_session("")).
       It is the active one whenever nothing is pinned. */
    var auto = document.createElement('button');
    auto.type = 'button';
    auto.className = 'chip ctrl' + (pinnedAppId ? '' : ' on');
    auto.title = "Follow the system's current session";
    auto.setAttribute('aria-label', "Follow the system's current session automatically");
    auto.textContent = 'AUTO';
    auto.addEventListener('click', function () { selectSession(''); });
    D.chips.appendChild(auto);

    if (!list.length) {
      var empty = document.createElement('span');
      empty.className = 'slab';
      empty.textContent = 'NO SESSIONS';
      D.chips.appendChild(empty);
      return;
    }
    for (var i = 0; i < list.length; i++) {
      (function (sess) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'chip ctrl' + (pinnedAppId && sess.app_id === pinnedAppId ? ' on' : '');
        b.title = (sess.title || '') + (sess.artist ? ' / ' + sess.artist : '');
        b.setAttribute('aria-label', 'Follow ' + (sess.app_name || 'session') +
          (sess.title ? ': ' + sess.title : ''));
        b.textContent = (sess.app_name || '?').toUpperCase();
        b.addEventListener('click', function () { selectSession(sess.app_id); });
        D.chips.appendChild(b);
      })(list[i]);
    }
  }

  /* --- marquee: seamless loop when the text overflows, static when it fits --- */
  function setMarquee(node, text) {
    if (!node) return;
    if (node.__txt === text) return;
    node.__txt = text;
    node.classList.remove('marq');
    node.style.removeProperty('--md');
    node.style.removeProperty('animation-duration');
    node.textContent = text;
    var clip = node.parentElement;
    /* measure the clip box with getBoundingClientRect too: under CSS zoom the
       client* box is in design px while rects are in on-screen px, and mixing
       the two would wrongly start every row scrolling */
    var avail = clip ? clip.getBoundingClientRect().width : 0;
    var need = node.getBoundingClientRect().width;
    if (!(need > avail + 1) || avail < 8) return;      // fits: stay static
    node.textContent = '';
    var a = document.createElement('span'); a.className = 'mc'; a.textContent = text;
    var b = document.createElement('span'); b.className = 'mc'; b.textContent = text;
    node.appendChild(a); node.appendChild(b);
    var dist = a.getBoundingClientRect().width;
    if (!(dist > 0)) { node.textContent = text; return; }
    node.style.setProperty('--md', dist.toFixed(1) + 'px');
    node.style.animationDuration = Math.max(5, dist / 24).toFixed(2) + 's';
    node.classList.add('marq');
  }

  /* The first measurement happens before the webfont swaps in, so a fallback-font
     width would wrongly start every row scrolling.  Re-measure once the font is
     really loaded and whenever the box changes size. */
  function remeasureMarquees() {
    [D.mTitle, D.mArtist, D.mAlbum, D.mStatus].forEach(function (n) {
      if (n) n.__txt = null;
    });
    render();
  }

  /* ---------------------------------------------------------- art */
  var artKey = undefined;         // undefined = nothing requested yet
  var artImg = null;              // the drawn <img> (null for the demo canvas)
  var artTrack = null;            // which track the drawn artwork belongs to
  var artDrawn = false;
  var duotoneOff = null;

  function artCtx() { return duotoneOff || (duotoneOff = document.createElement('canvas')); }

  function ramp(l) {
    // green duotone ramp: --bg -> --dim -> --base -> --bright
    var stops = [[6, 18, 10], [30, 122, 52], [51, 255, 102], [166, 255, 184]];
    var seg = [[0, 0.36], [0.36, 0.72], [0.72, 1]];
    for (var i = 0; i < 3; i++) {
      var lo = seg[i][0], hi = seg[i][1];
      if (l <= hi || i === 2) {
        var t = (l - lo) / (hi - lo);
        t = t < 0 ? 0 : (t > 1 ? 1 : t);
        var a = stops[i], b = stops[i + 1];
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
      }
    }
    return stops[3];
  }

  function pixelateAndTint(src) {
    // 1. coarse-grid downscale into a 44x44 buffer (imageSmoothing off)
    var off = artCtx();
    off.width = ART_GRID; off.height = ART_GRID;
    var octx = off.getContext('2d', { willReadFrequently: true });
    octx.clearRect(0, 0, ART_GRID, ART_GRID);
    octx.imageSmoothingEnabled = false;
    octx.drawImage(src, 0, 0, ART_GRID, ART_GRID);
    // 2. green duotone treatment
    try {
      var img = octx.getImageData(0, 0, ART_GRID, ART_GRID);
      var d = img.data;
      for (var i = 0; i < d.length; i += 4) {
        var l = (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255;
        var c = ramp(l);
        d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2]; d[i + 3] = 255;
      }
      octx.putImageData(img, 0, 0);
    } catch (e) { /* tainted source: fall through with the plain downscale */ }
    // 3. blow the coarse grid back up
    var cv = D.artcv;
    var ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, cv.width, cv.height);
  }

  function showPlaceholder(on) {
    D.artph.classList.toggle('on', !!on);
    D.arttag.hidden = true;
    if (on) {
      var ctx = D.artcv.getContext('2d');
      ctx.clearRect(0, 0, D.artcv.width, D.artcv.height);
      artImg = null;
      artTrack = null;
      artDrawn = false;
    }
  }

  /* procedural demo cover: deterministic shapes on a 44x44 grid, then the
     exact same pixelate + duotone pipeline the real artwork goes through */
  function demoArtSource() {
    var N = ART_GRID;
    var cv = document.createElement('canvas');
    cv.width = N; cv.height = N;
    var c = cv.getContext('2d');
    c.fillStyle = '#000'; c.fillRect(0, 0, N, N);
    var g = c.createRadialGradient(N * 0.5, N * 0.34, 1, N * 0.5, N * 0.34, N * 0.42);
    g.addColorStop(0, '#ffffff'); g.addColorStop(0.55, '#8a8a8a'); g.addColorStop(1, '#000000');
    c.fillStyle = g;
    c.beginPath(); c.arc(N * 0.5, N * 0.34, N * 0.24, 0, Math.PI * 2); c.fill();
    c.fillStyle = '#cfcfcf';
    c.fillRect(2, N * 0.56, N - 4, 1);
    var seed = 20261006;
    function rnd() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; }
    for (var x = 0; x < N;) {
      var w = 2 + Math.floor(rnd() * 4);
      var h = 2 + Math.floor(rnd() * 13);
      c.fillStyle = 'rgb(' + Math.floor(60 + rnd() * 195) + ',' +
        Math.floor(60 + rnd() * 195) + ',' + Math.floor(60 + rnd() * 195) + ')';
      c.fillRect(x, N * 0.58 - h, w, h + Math.floor(N * 0.42));
      x += w + 1;
    }
    for (var i = 0; i < 14; i++) {
      c.fillStyle = '#ffffff';
      c.fillRect(Math.floor(rnd() * N), Math.floor(rnd() * (N * 0.3)), 1, 1);
    }
    return cv;
  }

  function ensureArt(key, appId, title) {
    var ident = (appId || '') + '\u0000' + (title || '');
    if (key === artKey) return;                 // never re-fetch an unchanged art_key
    if (!key) {
      /* media.py documents that get_state() may report art_key:null for a single
         poll while it extracts artwork on its worker thread.  Hold the artwork
         already drawn for this same track instead of flashing the placeholder. */
      if (artDrawn && artTrack === ident) { artKey = key; return; }
      artKey = key;
      showPlaceholder(true);
      return;
    }
    artKey = key;
    if (isDemo()) {
      pixelateAndTint(demoArtSource());
      D.artph.classList.remove('on');
      D.arttag.hidden = false;
      D.arttag.textContent = 'DEMO';
      artTrack = ident;
      artDrawn = true;
      return;
    }
    call('get_art', [key]).then(function (url) {
      if (!url) { showPlaceholder(true); return; }
      var im = new Image();
      im.onload = function () {
        if (artKey !== key) return;
        artImg = im;
        artTrack = ident;
        artDrawn = true;
        D.artph.classList.remove('on');
        D.arttag.hidden = true;
        pixelateAndTint(im);
      };
      im.onerror = function () { showPlaceholder(true); };
      im.src = url;
    })['catch'](function () { showPlaceholder(true); });
  }

  /* ---------------------------------------------------------- commands */
  function refreshSoon() { setTimeout(poll, 30); }

  function togglePlay() {
    if (isDemo()) { demo.paused = !demo.paused; demo.base = demoPosition(); demo.t0 = nowMs(); render(); setMarquee(D.mStatus, (demo.paused ? 'paused' : 'playing').toUpperCase()); return; }
    call('play_pause').then(refreshSoon);
  }
  function skip(dir) {
    var name = dir > 0 ? 'next_track' : 'previous_track';
    if (isDemo() || S && (dir > 0 ? S.can_next === false : S.can_previous === false)) return;
    call(name).then(refreshSoon);
  }
  function setVolume(v) {
    v = clamp01(v);
    if (isDemo()) { demo.volume = v; demo.muted = false; render(); return; }
    call('set_volume', [v]).then(refreshSoon);
  }
  function toggleMute() {
    if (isDemo()) { demo.muted = !demo.muted; render(); return; }
    call('toggle_mute').then(refreshSoon);
  }
  function selectSession(id) {
    id = (id === null || id === undefined) ? '' : String(id);
    pinnedAppId = id;              // '' = auto-follow; drives the chip highlight
    renderedSessions = '';         // force the chips to re-render the active state
    if (isDemo()) {
      demo.activeApp = id || DEMO_SESSIONS[0].app_id;
      render();
      return;
    }
    render();
    call('select_session', [id]).then(refreshSoon);
  }
  function seekFraction(f) {
    f = clamp01(f);
    if (isDemo()) { demo.base = f * demo.duration; demo.t0 = nowMs(); seekPreview = null; render(); return; }
    if (!S || S.can_seek !== true) return;
    call('seek_fraction', [f]);
  }

  /* ---------------------------------------------------------- drag surfaces */
  function wireSurface(node, onFrac, canUse) {
    var dragging = false;
    function frac(ev) {
      var r = node.getBoundingClientRect();
      return clamp01((ev.clientX - r.left) / (r.width || 1));
    }
    node.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || !canUse()) return;
      e.preventDefault();
      dragging = true;
      try { node.setPointerCapture(e.pointerId); } catch (_) {}
      onFrac(frac(e), true);
    });
    node.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      onFrac(frac(e), false);
    });
    function end(e) {
      if (!dragging) return;
      dragging = false;
      try { node.releasePointerCapture(e.pointerId); } catch (_) {}
      onFrac(frac(e), false, true);
    }
    node.addEventListener('pointerup', end);
    node.addEventListener('pointercancel', end);
    node.addEventListener('lostpointercapture', function () { dragging = false; });
  }

  var lastSeekCall = 0;
  wireSurface(D.pbar, function (f, first, done) {
    if (!S || S.can_seek !== true) return;
    if (done || first) { seekPreview = null; seekFraction(f); return; }
    seekPreview = (S.duration ? f * S.duration : null);
    var t = nowMs();
    if (t - lastSeekCall > 90) { lastSeekCall = t; seekFraction(f); }
    render();
  }, function () { return !!(S && S.can_seek === true); });

  var lastVolCall = 0;
  wireSurface(D.vbar, function (f, first, done) {
    if (done || first) { setVolume(f); return; }
    var t = nowMs();
    if (t - lastVolCall > 60) { lastVolCall = t; setVolume(f); }
    if (isDemo()) { demo.volume = f; demo.muted = false; }
    D.vfill.style.width = (f * 100).toFixed(1) + '%';
    D.volPct.textContent = pct(f) + '%';
    D.vol.classList.remove('muted');
  }, function () { return true; });

  /* ---------------------------------------------------------- wiring */
  D.btnPlay.addEventListener('click', function () { togglePlay(); });
  D.btnPrev.addEventListener('click', function () { skip(-1); });
  D.btnNext.addEventListener('click', function () { skip(1); });
  D.btnMute.addEventListener('click', function () { toggleMute(); });

  /* frameless window: manual drag on the bezel, buttons keep their clicks */
  D.bezel.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest('.ctrl, button')) return;
    var b = bridge();
    if (b && typeof b.start_drag === 'function') {
      try { b.start_drag(); } catch (_) {}
    }
  });

  /* header window chrome: minimize + close (same clean path as tray items).
     Real <button>s, so the drag handler above skips them. */
  if (D.btnMin) {
    D.btnMin.addEventListener('click', function (e) {
      e.stopPropagation();
      var b = bridge();
      if (b && typeof b.minimize_app === 'function') {
        try { b.minimize_app(); } catch (_) {}
      }
    });
  }
  D.btnClose.addEventListener('click', function (e) {
    e.stopPropagation();
    var b = bridge();
    if (b && typeof b.quit_app === 'function') {
      try { b.quit_app(); } catch (_) {}
    }
  });

  /* bottom-right grip: JS-tracked resize.  pointerdown/move/up drive the
     bridge, which reads the OS cursor position and calls SetWindowPos, so the
     window really resizes with no frame drawn. */
  (function wireGrip() {
    var g = D.grip;
    if (!g) return;
    var active = false;
    g.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      var b = bridge();
      if (!b || typeof b.begin_resize !== 'function') return;   // demo: no window
      try { b.begin_resize(); } catch (_) {}
      active = true;
      try { g.setPointerCapture(e.pointerId); } catch (_) {}
    });
    g.addEventListener('pointermove', function () {
      if (!active) return;
      var b = bridge();
      if (!b || typeof b.resize_drag !== 'function') return;
      try { b.resize_drag(); } catch (_) {}
    });
    function end(e) {
      if (!active) return;
      active = false;
      try { g.releasePointerCapture(e.pointerId); } catch (_) {}
      var b = bridge();
      if (b && typeof b.end_resize === 'function') {
        try { b.end_resize(); } catch (_) {}
      }
      applyZoom();
    }
    g.addEventListener('pointerup', end);
    g.addEventListener('pointercancel', end);
  })();

  window.addEventListener('keydown', function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    var k = e.key;
    if (k === ' ' || k === 'Spacebar' || e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (k === 'ArrowRight') { e.preventDefault(); skip(1); }
    else if (k === 'ArrowLeft') { e.preventDefault(); skip(-1); }
    else if (k === 'ArrowUp') { e.preventDefault(); nudgeVolume(0.05); }
    else if (k === 'ArrowDown') { e.preventDefault(); nudgeVolume(-0.05); }
    else if (k === 'm' || k === 'M') { e.preventDefault(); toggleMute(); }
  });

  function nudgeVolume(d) {
    var cur = (S && S.volume !== null && S.volume !== undefined) ? S.volume : (demo.volume || 0);
    setVolume(cur + d);
  }

  /* ---------------------------------------------------------- clock (10) */
  function clockText() {
    var d = new Date();
    return (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes();
  }

  /* ---------------------------------------------------------- boot (6) */
  var BOOT_LINES = ['CRT-MEDIA v1.0', 'PHOSPHOR P1 ... OK', 'SMTC LINK ... OK', 'AUDIO BUS ... OK'];
  var bootStarted = nowMs();
  var bootDone = false;

  function bootFrame() {
    if (bootDone) return;
    var t = nowMs() - bootStarted;
    var total = BOOT_LINES.join('\n').length;
    var n = Math.min(total, Math.floor((t / BOOT_TYPE_MS) * total));
    var flat = BOOT_LINES.join('\n');
    var shown = flat.slice(0, n);
    paintBoot(shown, n < total);
    if (t >= BOOT_TYPE_MS + BOOT_HOLD_MS) finishBoot(false);
    else requestAnimationFrame(bootFrame);
  }

  function paintBoot(shown, typing) {
    var esc = shown.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    esc = esc.replace(/OK/g, '<span class="ok">OK</span>');
    if (typing) esc += '<span class="ok">_</span>';
    D.bootLines.innerHTML = esc;
  }

  function finishBoot(skipped) {
    if (bootDone) return;
    bootDone = true;
    if (skipped) paintBoot(BOOT_LINES.join('\n'), false);
    D.boot.classList.add('fade');
    setTimeout(function () { D.boot.classList.add('gone'); }, 200);
  }

  function skipBoot() { if (!bootDone) finishBoot(true); }
  window.addEventListener('pointerdown', skipBoot, true);
  window.addEventListener('keydown', skipBoot, true);

  /* ---------------------------------------------------------- poll loop */
  var POLL_DEADLINE_MS = 3000;   // a poll that has not settled by now is a failure
  var polling = false;
  var pollSeq = 0;
  function poll() {
    if (polling) return;
    if (isDemo()) {
      apply(demoState(), nowMs(), nowMs());
      return;
    }
    polling = true;
    var seq = ++pollSeq;
    var t0 = nowMs();
    /* Deadline: a promise that never settles must not freeze the display
       forever.  If it has not resolved in POLL_DEADLINE_MS, clear the in-flight
       guard and show the error link state so the next tick can try again. */
    var timer = setTimeout(function () {
      if (seq !== pollSeq || !polling) return;   // already settled / superseded
      polling = false;
      link = 'ERR';
      render();
    }, POLL_DEADLINE_MS);
    function settle(fn) {
      if (seq !== pollSeq || !polling) return;   // timed out, or superseded
      clearTimeout(timer);
      polling = false;
      fn();
    }
    var p;
    try { p = bridge().get_state(); } catch (e) { p = null; }
    Promise.resolve(p).then(function (st) {
      var t1 = nowMs();
      settle(function () {
        if (st && typeof st === 'object') apply(st, t0, t1);
        else { link = 'ERR'; render(); }
      });
    }, function () {
      settle(function () { link = 'ERR'; render(); });
    });
  }

  /* animate the bar between polls while playing */
  function tick() {
    if (S && S.status === 'playing' && !D.pbar.classList.contains('na')) {
      var pos = positionNow();
      var dur = S.duration;
      if (pos !== null && dur) {
        var f = clamp01(pos / dur);
        D.pfill.style.width = (f * 100).toFixed(2) + '%';
        D.phead.style.left = (f * 100).toFixed(2) + '%';
        D.tElapsed.textContent = fmtTime(pos);
      }
    }
    if (clockDirty()) render();
    requestAnimationFrame(tick);
  }

  var lastClock = '';
  function clockDirty() {
    var c = clockText();
    if (c !== lastClock) { lastClock = c; return true; }
    return false;
  }

  /* ---------------------------------------------------------- visualizer
     A deliberately FAKE audio visualizer.  It is NOT analysing audio and it
     does not claim to: its amplitude is the system volume level that
     get_state() already reports and that the volume meter renders, plus a
     smoothed, randomly refreshed synthetic bounce per bar.  Dragging the
     volume slider drives it immediately because both read the same state
     value.  It is honest about transport state:
       volume 0 / muted      -> every bar collapses to the baseline
       paused / stopped      -> a small dim idle jitter at about 8% height
       no media session      -> a flat dim baseline
     The band has a fixed height and the bars are absolutely positioned
     inside it, so nothing here can grow the page or add a scrollbar. */
  var VIZ_BARS = 24;
  var VIZ_SEG = 4;          // design px per LED segment (height quantisation)
  var VIZ_MAX = 24;         // 6 segments; the bar area is 24 design px
  var VIZ_HOLD_MS = 1000;   // peak cap falls to the baseline over ~1 s
  var vizBuilt = false;
  var vizBars = [];
  var vizPrevT = 0;

  function vizBuild() {
    var host = D.viz;
    if (!host || vizBuilt) return;
    host.textContent = '';
    for (var i = 0; i < VIZ_BARS; i++) {
      var vb = document.createElement('div'); vb.className = 'vb';
      var led = document.createElement('div'); led.className = 'led';
      var fill = document.createElement('div'); fill.className = 'fill';
      led.appendChild(fill);
      var cap = document.createElement('div'); cap.className = 'cap';
      vb.appendChild(led);
      vb.appendChild(cap);
      host.appendChild(vb);
      vizBars.push({ led: led, cap: cap, h: 0, peak: 0,
                     amp: 0.5, jit: Math.random(), nextT: 0 });
    }
    vizBuilt = true;
  }

  /* which honest mode the current state implies, and the amplitude (volume) */
  function vizState() {
    var s = S;
    if (!s || !s.has_session) return { mode: 'flat', vol: 0 };
    var vol = (s.volume === null || s.volume === undefined) ? 0 : clamp01(s.volume);
    if (s.muted || vol <= 0.0005) return { mode: 'flat', vol: 0 };
    if (s.status !== 'playing') return { mode: 'idle', vol: vol };
    return { mode: 'live', vol: vol };
  }

  function vizFrame(t) {
    if (!vizBuilt) { requestAnimationFrame(vizFrame); return; }
    var dt = t - vizPrevT;
    vizPrevT = t;
    if (!(dt > 0) || dt > 250) dt = 16.7;          // first frame / tab wake
    var m = vizState();
    var k = 1 - Math.pow(0.03, dt / 220);          // eased, frame-rate aware
    if (!(k > 0)) k = 0.2;
    if (k > 0.4) k = 0.4;
    var decay = VIZ_MAX * (dt / VIZ_HOLD_MS);

    for (var i = 0; i < vizBars.length; i++) {
      var b = vizBars[i];
      if (t >= b.nextT) {                          // fresh target every 120-200 ms
        b.nextT = t + 120 + Math.random() * 80;
        b.amp = 0.35 + Math.random() * 0.65;
        b.jit = Math.random();
      }
      var target;
      if (m.mode === 'flat') target = 0;
      else if (m.mode === 'idle') target = 1.0 + b.jit * 1.0;             // ~8% of 24
      else target = m.vol * VIZ_MAX * b.amp;
      b.h += (target - b.h) * k;
      if (b.h < 0) b.h = 0;

      var qh;
      if (m.mode === 'live') qh = Math.round(b.h / VIZ_SEG) * VIZ_SEG;    // LED steps
      else if (m.mode === 'idle') qh = Math.max(1, Math.round(b.h));      // dim 1-2 px
      else qh = 0;                                                       // baseline
      if (qh > VIZ_MAX) qh = VIZ_MAX;
      b.led.style.height = qh + 'px';

      /* peak-hold cap: jumps to the bar top, then decays slowly */
      var top = Math.min(b.h, VIZ_MAX);
      if (top > b.peak) b.peak = top;
      else { b.peak -= decay; if (b.peak < 0) b.peak = 0; }
      if (m.mode === 'live' && b.peak >= qh + 2) {
        var cq = Math.round(b.peak / VIZ_SEG) * VIZ_SEG;
        if (cq < qh + 2) cq = qh + 2;
        if (cq > VIZ_MAX) cq = VIZ_MAX;
        b.cap.style.bottom = cq + 'px';
        b.cap.style.opacity = '1';
      } else {
        b.cap.style.opacity = '0';
      }
    }
    D.viz.classList.toggle('idle', m.mode === 'idle');
    D.viz.classList.toggle('flat', m.mode === 'flat');
    requestAnimationFrame(vizFrame);
  }

  /* ---------------------------------------------------------- selftest */
  function toRgb(str) {
    var m = /rgba?\(([^)]+)\)/.exec(str || '');
    if (!m) return null;
    var p = m[1].split(',').map(function (x) { return parseFloat(x); });
    return [p[0], p[1], p[2]];
  }
  function lum(rgb) {
    var c = rgb.map(function (v) {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function contrast(a, b) {
    var la = lum(a), lb = lum(b);
    var hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }
  function measureFamily(fam) {
    var s = document.createElement('span');
    s.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;white-space:pre;' +
      'font-size:40px;font-family:' + fam;
    s.textContent = 'MWWWiii1234567890';
    document.body.appendChild(s);
    var w = s.getBoundingClientRect().width;
    if (s.parentNode) s.parentNode.removeChild(s);
    return Math.round(w * 100) / 100;
  }
  function rectOf(sel) {
    var n = document.querySelector(sel);
    if (!n) return null;
    var r = n.getBoundingClientRect();
    /* getBoundingClientRect is reported in the on-screen (zoomed) space; report
       design-space geometry so the overflow test means the same thing at every
       window size.  The reference box is the design canvas, widened only if the
       window's aspect ratio makes the layout box bigger (the existing flexible
       spacer/meta absorb that slack, so it is not an overflow of anything). */
    var z = rootZoom || 1;
    var boxW = Math.max(CANVAS_W, (window.innerWidth || CANVAS_W) / z);
    var boxH = Math.max(CANVAS_H, (window.innerHeight || CANVAS_H) / z);
    var x = Math.round((r.left / z) * 100) / 100, y = Math.round((r.top / z) * 100) / 100;
    var w = Math.round((r.width / z) * 100) / 100, h = Math.round((r.height / z) * 100) / 100;
    return {
      sel: sel, x: x, y: y, w: w, h: h,
      right: Math.round((x + w) * 100) / 100, bottom: Math.round((y + h) * 100) / 100,
      overflows: (x < -0.5 || y < -0.5 || (x + w) > boxW + 0.5 || (y + h) > boxH + 0.5)
    };
  }

  function computeSelftest() {
    var doc = document.documentElement;
    var body = document.body;
    var cs = getComputedStyle(D.statusText);
    var panelBg = getComputedStyle(D.artframe).backgroundColor;
    var txt = toRgb(cs.color);
    var pan = toRgb(panelBg);
    var cr = (txt && pan) ? Math.round(contrast(txt, pan) * 100) / 100 : null;

    var sel = ['.bezel', '.screen', '.hdr', '.hdr-txt', '.hdr-right', '.hdr-min', '.hdr-close',
      '.statusline', '.cursor',
      '.content', '.now', '.artframe', '.artcv', '.meta', '.mrow', '.clip',
      '.prog', '.pbar', '.transport', '#btnPrev', '#btnPlay', '#btnNext',
      '.vol', '#vbar', '.viz', '.viz .vb', '.sessions', '#chips', '#btnMute', '.spacer', '.grip'];
    var els = sel.map(rectOf).filter(Boolean);
    var over = els.filter(function (e) { return e.overflows; }).map(function (e) { return e.sel; });

    var tbs = Array.prototype.slice.call(document.querySelectorAll('.tb'));
    var minW = Infinity, minH = Infinity;
    tbs.forEach(function (b) {
      /* offset* are layout pixels: unaffected by the CSS zoom, so the 44x36
         contract is measured against the design, whatever the window size */
      minW = Math.min(minW, b.offsetWidth);
      minH = Math.min(minH, b.offsetHeight);
    });
    var ctrls = Array.prototype.slice.call(document.querySelectorAll('.ctrl'));
    var cMinW = Infinity, cMinH = Infinity;
    ctrls.forEach(function (b) {
      cMinW = Math.min(cMinW, b.offsetWidth);
      cMinH = Math.min(cMinH, b.offsetHeight);
    });

    var wCRT = measureFamily("'CRT'");
    var wMono = measureFamily('monospace');
    var wCons = measureFamily('Consolas');
    var fonts = (window.document.fonts && document.fonts.check) ? document.fonts.check('19px CRT') : null;
    var loaded = fonts === true && wCRT !== wMono && wCRT !== wCons;

    var pos = positionNow();
    var st = S || (isDemo() ? demoState() : EMPTY_STATE);
    var bootStyle = getComputedStyle(D.boot);

    return {
      canvas: { w: CANVAS_W, h: CANVAS_H },
      layoutBox: {
        w: Math.round(Math.max(CANVAS_W, window.innerWidth / (rootZoom || 1)) * 100) / 100,
        h: Math.round(Math.max(CANVAS_H, window.innerHeight / (rootZoom || 1)) * 100) / 100
      },
      viewport: {
        innerInner: { w: window.innerWidth, h: window.innerHeight },
        docClient: { w: doc.clientWidth, h: doc.clientHeight },
        dpr: window.devicePixelRatio || 1
      },
      zoom: {
        scale: Math.round(rootZoom * 10000) / 10000,
        css: document.documentElement.style.zoom || '1',
        computed: getComputedStyle(document.documentElement).zoom
      },
      font: {
        family: cs.fontFamily,
        fontsCheck: fonts,
        loaded: loaded,
        widthCRT: wCRT, widthMonospace: wMono, widthConsolas: wCons,
        differsFromFallbacks: (wCRT !== wMono && wCRT !== wCons)
      },
      colors: {
        textColor: cs.color,
        panelBackground: panelBg,
        contrastRatio: cr,
        contrastOk: cr !== null && cr >= 4.5
      },
      scroll: {
        docScrollW: doc.scrollWidth, docClientW: doc.clientWidth,
        docScrollH: doc.scrollHeight, docClientH: doc.clientHeight,
        bodyScrollW: body.scrollWidth, bodyClientW: body.clientWidth,
        bodyScrollH: body.scrollHeight, bodyClientH: body.clientHeight,
        overflowX: doc.scrollWidth > doc.clientWidth,
        overflowY: doc.scrollHeight > doc.clientHeight
      },
      elements: els,
      overflow: { any: over.length > 0, offenders: over },
      transport: {
        count: tbs.length,
        minHitW: isFinite(minW) ? Math.round(minW * 100) / 100 : null,
        minHitH: isFinite(minH) ? Math.round(minH * 100) / 100 : null,
        ok: isFinite(minW) && minW >= 44 && minH >= 36,
        allControls: {
          count: ctrls.length,
          minW: isFinite(cMinW) ? Math.round(cMinW * 100) / 100 : null,
          minH: isFinite(cMinH) ? Math.round(cMinH * 100) / 100 : null
        }
      },
      state: {
        mode: isDemo() ? 'demo' : 'live',
        link: link,
        status: st.status,
        position: pos,
        duration: st.duration,
        rendered: {
          elapsed: D.tElapsed.textContent,
          total: D.tTotal.textContent,
          barWidthPercent: D.pfill.style.width,
          barUnavailable: D.pbar.classList.contains('na'),
          volumePercent: D.volPct.textContent,
          statusLine: D.statusText.textContent
        },
        lines: {
          title: String(D.mTitle.__txt || '').slice(0, 60),
          artist: String(D.mArtist.__txt || '').slice(0, 60),
          album: String(D.mAlbum.__txt || '').slice(0, 60),
          titleMarquee: D.mTitle.classList.contains('marq'),
          artistMarquee: D.mArtist.classList.contains('marq'),
          albumMarquee: D.mAlbum.classList.contains('marq'),
          titleTextWidth: Math.round(D.mTitle.scrollWidth),
          titleClipWidth: D.mTitle.parentElement ? D.mTitle.parentElement.clientWidth : null
        }
      },
      art: {
        artKey: artKey === undefined ? null : artKey,
        placeholder: D.artph.classList.contains('on'),
        tagged: !D.arttag.hidden,
        drawn: artDrawn
      },
      boot: {
        done: bootDone,
        opacity: bootStyle.opacity,
        display: bootStyle.display
      },
      sessions: {
        count: document.querySelectorAll('.chip').length,
        active: (function () {
          var a = document.querySelector('.chip.on');
          return a ? a.textContent : null;
        })()
      },
      build: 'CRT-MEDIA ui-1'
    };
  }

  function selftestTick() {
    try { window.__SELFTEST__ = computeSelftest(); } catch (e) { window.__SELFTEST__ = { error: String(e) }; }
    setTimeout(selftestTick, 400);
  }

  /* ---------------------------------------------------------- window zoom
     The design is a fixed 360x400.  A resized window is filled by scaling the
     whole design with CSS `zoom` on the document root (not transform:scale,
     which would blur the glyphs).  The scale is the smaller of the two axis
     ratios, so the design always covers the window and is never clipped;
     because the page lays out at viewport/zoom, squarer windows keep their
     exact 360x400 proportions. */
  var rootZoom = 1;

  function applyZoom() {
    var w = window.innerWidth || CANVAS_W;
    var h = window.innerHeight || CANVAS_H;
    var z = Math.min(w / CANVAS_W, h / CANVAS_H);
    if (!(z > 0) || !isFinite(z)) z = 1;
    rootZoom = z;
    try {
      document.documentElement.style.zoom = (z === 1) ? '' : String(z);
    } catch (e) { /* no CSS zoom support: the layout just stays at design size */ }
  }
  /* app.py calls this once after its startup viewport sizing settles */
  window.__crtApplyZoom = applyZoom;

  /* ---------------------------------------------------------- boot up */
  function start() {
    paintBoot('', true);
    applyZoom();                             // scale the design to this window
    vizBuild();                              // 24 LED bars in the visualizer band
    requestAnimationFrame(bootFrame);
    poll();                                  // first poll is NOT delayed by the boot
    setInterval(poll, POLL_MS);

    /* re-decide the marquees once the webfont is available */
    try {
      if (window.document.fonts) {
        if (document.fonts.ready && document.fonts.ready.then) {
          document.fonts.ready.then(function () { setTimeout(remeasureMarquees, 0); });
        }
        if (document.fonts.addEventListener) {
          document.fonts.addEventListener('loadingdone', function () { setTimeout(remeasureMarquees, 0); });
        }
      }
    } catch (e) { /* ignore */ }
    window.addEventListener('resize', function () { applyZoom(); remeasureMarquees(); });
    setTimeout(remeasureMarquees, 1200);     // belt and braces: font swap may be silent

    if (SELFTEST_ON) {
      window.__selftest = function () { return JSON.stringify(computeSelftest()); };
      selftestTick();
    }
    requestAnimationFrame(tick);
    requestAnimationFrame(vizFrame);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.addEventListener('pywebviewready', function () { if (!FORCE_DEMO) { artKey = undefined; poll(); } });
})();
