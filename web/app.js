(() => {
  const $ = (id) => document.getElementById(id);

  const FOLLOW_KEY = "bw_follow_current";
  const state = {
    token: localStorage.getItem("bw_api_token") || "",
    pollTimer: null,
    sessionId: localStorage.getItem("bw_session_id") || "",
    settings: null,
    lastLoggedStatus: "",
    lastSession: null,
    showRawJson: false,
    lastQrContent: "",
    loginRequired: false,
    consoleUser: null,
    bootstrap: null,
    followCurrent: localStorage.getItem(FOLLOW_KEY) !== "0",
    queue: null,
    refreshing: false,
  };

  $("apiToken").value = state.token;
  if (state.sessionId) $("sessionId").value = state.sessionId;
  $("followCurrent").checked = state.followCurrent;

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    return headers;
  }

  function goLogin() {
    const next = encodeURIComponent(location.pathname + location.search);
    location.replace(`/login?next=${next}`);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      credentials: "same-origin",
      headers: { ...authHeaders(), ...(opts.headers || {}) },
    });
    if (res.status === 401 && state.loginRequired) {
      goLogin();
      throw new Error("未登录");
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    if (!res.ok) {
      const detail = data?.detail || data?.message || text || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function applyAuthMode(boot, me) {
    state.bootstrap = boot;
    state.loginRequired = !!(boot.login_required ?? boot.basic_auth_required);
    state.consoleUser = me?.username || null;
    const label = $("authModeLabel");
    const tokenField = $("tokenField");
    const btnSave = $("btnSaveToken");
    const btnLogout = $("btnLogout");
    if (state.loginRequired) {
      label.textContent = state.consoleUser ? `已登录 · ${state.consoleUser}` : "已登录";
      tokenField.classList.add("hidden");
      btnSave.classList.add("hidden");
      btnLogout.classList.remove("hidden");
    } else {
      label.textContent = "Bearer Token";
      tokenField.classList.remove("hidden");
      btnSave.classList.remove("hidden");
      btnLogout.classList.add("hidden");
    }
  }

  function appendLog(line) {
    const el = $("logView");
    let text = String(line ?? "");
    // 后端已带 [YYYY-MM-DD HH:MM:SS]；前端本地提示补上时间
    if (!/^\[\d{4}-\d{2}-\d{2} /.test(text)) {
      const d = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      const ts =
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
      text = `[${ts}] ${text}`;
    }
    el.textContent += (el.textContent ? "\n" : "") + text;
    el.scrollTop = el.scrollHeight;
  }

  function applyVncFrame(url) {
    $("vncUrlLabel").textContent = url || "(未配置)";
    const frame = $("vncFrame");
    const empty = $("vncEmpty");
    if (!url) {
      frame.removeAttribute("src");
      empty.classList.remove("hidden");
      return;
    }
    let finalUrl = url;
    try {
      const u = new URL(url, window.location.origin);
      const viaProxy = u.pathname.startsWith("/vnc");
      if (!u.searchParams.has("autoconnect")) u.searchParams.set("autoconnect", "true");
      if (!u.searchParams.has("resize")) u.searchParams.set("resize", "scale");
      if (viaProxy && !u.searchParams.get("path")) {
        u.searchParams.set("path", "vnc/websockify");
      }
      finalUrl = u.toString();
    } catch {
      /* keep raw */
    }
    frame.src = finalUrl;
    empty.classList.add("hidden");
  }

  async function loadSettings() {
    const data = await api("/v1/console/settings");
    state.settings = data;
    const fallback =
      state.bootstrap?.default_vnc_page_url ||
      "/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify";
    $("vncPageUrl").value = data.vnc_page_url || fallback;
    $("defaultWebhook").value = data.default_webhook_url || "";
    $("webhookUrl").value = data.default_webhook_url || $("webhookUrl").value;
    const pri = data.default_priority || 1;
    $("defaultPriority").value = pri;
    $("testPriority").value = pri;
    applyVncFrame(data.vnc_page_url || fallback);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function summarizeForJson(data, raw) {
    if (raw) return data;
    const out = structuredClone(data);
    const ch = out?.challenge;
    if (ch && typeof ch.image_base64 === "string" && ch.image_base64.length > 80) {
      ch.image_base64 = `<base64 omitted ${ch.image_base64.length} chars>`;
    }
    const cookies = out?.result?.cookies;
    if (Array.isArray(cookies) && cookies.length > 0) {
      out.result.cookies = cookies.map((c) => ({
        name: c.name,
        domain: c.domain,
        path: c.path,
        value: typeof c.value === "string" && c.value.length > 24
          ? `${c.value.slice(0, 12)}…(${c.value.length})`
          : c.value,
      }));
    }
    return out;
  }

  function highlightJson(value) {
    const json = escapeHtml(JSON.stringify(value, null, 2));
    return json.replace(
      /("(?:\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
      (match) => {
        let cls = "json-number";
        if (match.startsWith('"')) {
          cls = /:$/.test(match) ? "json-key" : "json-string";
        } else if (match === "true" || match === "false") {
          cls = "json-bool";
        } else if (match === "null") {
          cls = "json-null";
        }
        return `<span class="${cls}">${match}</span>`;
      }
    );
  }

  function renderSessionJson(data) {
    const view = summarizeForJson(data, state.showRawJson);
    $("sessionView").innerHTML = highlightJson(view);
    $("btnToggleRawJson").textContent = state.showRawJson ? "精简显示" : "显示完整";
  }

  function renderStatusBanner(data) {
    const el = $("statusBanner");
    el.classList.remove("idle", "ok", "bad", "wait", "run");
    const st = data.status || "";
    const cancelling = !!data.cancel_requested && !["succeeded", "failed", "cancelled"].includes(st);
    if (cancelling) {
      el.classList.add("wait");
      el.textContent = "取消中…（等待 worker 退出当前步骤）";
    } else if (st === "succeeded") {
      el.classList.add("ok");
      const n = data.result?.cookies?.length ?? 0;
      el.textContent = `登录成功 · cookies=${n}`;
    } else if (st === "failed" || st === "cancelled") {
      el.classList.add("bad");
      const code = data.error?.code || st;
      const msg = data.error?.message || "";
      el.textContent = `登录失败 · ${code}${msg ? " · " + msg : ""}`;
    } else if (st === "waiting_captcha" || st === "waiting_security") {
      el.classList.add("wait");
      el.textContent = st === "waiting_captcha" ? "等待人工验证码" : "等待安全扫码";
    } else if (st === "running" || st === "queued") {
      el.classList.add("run");
      el.textContent = st === "queued" ? "排队中" : "登录进行中…";
    } else {
      el.classList.add("run");
      el.textContent = `状态：${st || "未知"}`;
    }
    const btn = $("btnCancel");
    if (btn) {
      const terminal = ["succeeded", "failed", "cancelled"].includes(st);
      btn.disabled = terminal || cancelling;
      btn.textContent = cancelling ? "取消中…" : "取消";
    }
  }

  function renderQueue(q) {
    state.queue = q;
    $("qQueued").textContent = String(q.queued ?? 0);
    $("qRunning").textContent = String(q.running ?? 0);
    $("qWaiting").textContent = String(q.waiting ?? 0);
  }

  function setSessionId(id, { persist = true } = {}) {
    const next = (id || "").trim();
    if (!next) return;
    $("sessionId").value = next;
    state.sessionId = next;
    if (persist) localStorage.setItem("bw_session_id", next);
  }

  async function refreshSession(idOverride) {
    const id = (idOverride || $("sessionId").value).trim();
    if (!id) return null;
    if (state.refreshing) return state.lastSession;
    state.refreshing = true;
    try {
      setSessionId(id);
      const data = await api(`/v1/sessions/${id}`);
      state.lastSession = data;
      renderSessionJson(data);
      renderStatusBanner(data);
      renderChallenge(data);
      const key = `${data.id}:${data.status}:${data.cancel_requested ? 1 : 0}:${data.error?.code || ""}`;
      if (key !== state.lastLoggedStatus) {
        state.lastLoggedStatus = key;
        appendLog(
          `会话 ${data.id} => ${data.status}` +
            (data.cancel_requested && data.status !== "cancelled" ? " (取消中)" : "") +
            (data.error ? ` code=${data.error.code} message=${data.error.message}` : "") +
            (data.result?.cookies ? ` cookies=${data.result.cookies.length}` : "")
        );
      }
      return data;
    } finally {
      state.refreshing = false;
    }
  }

  function renderChallenge(data) {
    const box = $("challengeBox");
    const img = $("challengeImg");
    const canvas = $("qrCanvas");
    const form = $("captchaForm");
    const meta = $("challengeMeta");

    const ch = data.challenge;
    img.classList.add("hidden");
    canvas.classList.add("hidden");
    form.classList.add("hidden");

    if (!ch || !["waiting_captcha", "waiting_security"].includes(data.status)) {
      box.classList.add("is-empty");
      meta.textContent = "无挑战";
      state.lastQrContent = "";
      return;
    }

    box.classList.remove("is-empty");
    meta.textContent = `${ch.type || ""} · ${ch.expires_at || ""}`;

    if (data.status === "waiting_captcha") {
      form.classList.remove("hidden");
      if (ch.image_base64) {
        img.src = ch.image_base64;
        img.classList.remove("hidden");
      }
    }

    if (data.status === "waiting_security") {
      if (ch.qr_content && window.QRCode) {
        canvas.classList.remove("hidden");
        const size = 168;
        if (ch.qr_content !== state.lastQrContent || canvas.width !== size) {
          state.lastQrContent = ch.qr_content;
          QRCode.toCanvas(
            canvas,
            ch.qr_content,
            { width: size, margin: 1, errorCorrectionLevel: "M" },
            () => {}
          );
        }
      } else {
        meta.textContent += " · 缺少 qr_content";
      }
    } else {
      state.lastQrContent = "";
    }
  }

  async function pollTick() {
    try {
      const q = await api("/v1/queue");
      renderQueue(q);
      const currentId = q.current_session_id || "";
      if (state.followCurrent && currentId && currentId !== $("sessionId").value.trim()) {
        setSessionId(currentId);
        appendLog(`跟随当前处理会话 ${currentId}`);
      }
    } catch (e) {
      /* queue poll soft-fail */
    }

    const id = $("sessionId").value.trim();
    if (id) {
      await refreshSession(id).catch((e) => appendLog(`poll error: ${e.message}`));
    }
  }

  function startPoll() {
    stopPoll();
    state.pollTimer = setInterval(() => {
      pollTick();
    }, 2000);
  }

  function stopPoll() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  let ws;
  function connectLogs() {
    if (ws) {
      try { ws.close(); } catch { /* */ }
    }
    if (!state.loginRequired && !state.token) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let url = `${proto}://${location.host}/v1/logs`;
    if (!state.loginRequired && state.token) {
      url += `?token=${encodeURIComponent(state.token)}`;
    }
    ws = new WebSocket(url);
    ws.onmessage = (ev) => appendLog(ev.data);
    ws.onclose = () => appendLog("日志 WS 已断开");
  }

  async function pingHealth() {
    try {
      const res = await fetch("/health");
      $("healthDot").className = "dot " + (res.ok ? "ok" : "bad");
    } catch {
      $("healthDot").className = "dot bad";
    }
  }

  $("followCurrent").onchange = () => {
    state.followCurrent = $("followCurrent").checked;
    localStorage.setItem(FOLLOW_KEY, state.followCurrent ? "1" : "0");
    if (state.followCurrent) pollTick();
  };

  $("sessionId").addEventListener("change", () => {
    const id = $("sessionId").value.trim();
    if (id) {
      state.followCurrent = false;
      $("followCurrent").checked = false;
      localStorage.setItem(FOLLOW_KEY, "0");
      setSessionId(id);
      refreshSession(id).catch((e) => appendLog(e.message));
    }
  });

  $("btnSaveToken").onclick = () => {
    state.token = $("apiToken").value.trim();
    localStorage.setItem("bw_api_token", state.token);
    connectLogs();
    loadSettings().catch((e) => appendLog(e.message));
    startPoll();
    appendLog("Token 已保存到本机 localStorage");
  };

  $("btnLogout").onclick = async () => {
    try {
      await api("/v1/auth/logout", { method: "POST", body: "{}" });
    } catch {
      /* still redirect */
    }
    goLogin();
  };

  $("btnTestLogin").onclick = () => {
    $("createMsg").textContent = "";
    $("testLoginDialog").showModal();
  };
  $("btnCloseTest").onclick = () => $("testLoginDialog").close();

  $("btnSettings").onclick = () => $("settingsDialog").showModal();

  $("btnSaveSettings").onclick = async () => {
    try {
      const body = {
        vnc_page_url: $("vncPageUrl").value.trim(),
        default_webhook_url: $("defaultWebhook").value.trim(),
        default_priority: Number($("defaultPriority").value || 1),
      };
      await api("/v1/console/settings", { method: "PUT", body: JSON.stringify(body) });
      await loadSettings();
      appendLog("控制台设置已保存");
      $("settingsDialog").close();
    } catch (e) {
      appendLog(`保存设置失败: ${e.message}`);
    }
  };

  $("btnReloadVnc").onclick = () => loadSettings().catch((e) => appendLog(e.message));
  $("btnOpenVnc").onclick = () => {
    const url = state.settings?.vnc_page_url;
    if (url) window.open(url, "_blank");
  };

  $("loginForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = {
      username: fd.get("username"),
      password: fd.get("password"),
      webhook_url: fd.get("webhook_url") || null,
      client_ref: fd.get("client_ref") || null,
      priority: Number(fd.get("priority") || 1),
      silent: fd.get("silent") === "on",
    };
    if (!body.webhook_url) delete body.webhook_url;
    if (!body.client_ref) delete body.client_ref;
    try {
      const data = await api("/v1/sessions", { method: "POST", body: JSON.stringify(body) });
      $("createMsg").textContent = `已创建 ${data.id} · queue_position=${data.queue_position}`;
      setSessionId(data.id);
      // 新提交后短暂跟随该会话；若开启跟随且已有进行中任务，下一轮会切回 current
      await refreshSession(data.id);
      startPoll();
      appendLog(`已创建测试会话 ${data.id}`);
      $("testLoginDialog").close();
    } catch (e) {
      $("createMsg").textContent = e.message;
    }
  };

  $("btnToggleRawJson").onclick = () => {
    state.showRawJson = !state.showRawJson;
    if (state.lastSession) renderSessionJson(state.lastSession);
  };
  $("btnCopyJson").onclick = async () => {
    if (!state.lastSession) return;
    const text = JSON.stringify(
      summarizeForJson(state.lastSession, state.showRawJson),
      null,
      2
    );
    try {
      await navigator.clipboard.writeText(text);
      appendLog("会话 JSON 已复制");
    } catch (e) {
      appendLog(`复制失败: ${e.message}`);
    }
  };

  $("btnRefresh").onclick = () => refreshSession().catch((e) => appendLog(e.message));
  $("btnCancel").onclick = async () => {
    const id = $("sessionId").value.trim();
    if (!id) return;
    const btn = $("btnCancel");
    btn.disabled = true;
    btn.textContent = "取消中…";
    try {
      const data = await api(`/v1/sessions/${id}`, { method: "DELETE" });
      appendLog(
        data.status === "cancelled"
          ? `会话 ${id} 已取消`
          : `会话 ${id} 已请求取消，等待 worker 退出`
      );
      await refreshSession();
      startPoll();
    } catch (e) {
      appendLog(e.message);
      btn.disabled = false;
      btn.textContent = "取消";
    }
  };

  $("captchaForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const id = $("sessionId").value.trim();
    const code = new FormData(ev.target).get("code");
    try {
      await api(`/v1/sessions/${id}/captcha`, {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      appendLog("验证码已提交");
      ev.target.reset();
      await refreshSession();
    } catch (e) {
      appendLog(e.message);
    }
  };

  pingHealth();
  setInterval(pingHealth, 10000);

  (async () => {
    try {
      const boot = await fetch("/v1/console/bootstrap", { credentials: "same-origin" }).then((r) =>
        r.json()
      );
      const me = await fetch("/v1/auth/me", { credentials: "same-origin" })
        .then((r) => r.json())
        .catch(() => ({ authenticated: false }));
      if ((boot.login_required ?? boot.basic_auth_required) && !me.authenticated) {
        goLogin();
        return;
      }
      applyAuthMode(boot, me);
      if (!state.loginRequired && !state.token) {
        appendLog("未启用控制台登录：请填写 API Token 并保存");
        return;
      }
      if (state.loginRequired) {
        appendLog(`已登录控制台${state.consoleUser ? " · " + state.consoleUser : ""}；VNC 走 /vnc 同源代理`);
      }
      connectLogs();
      await loadSettings();
      startPoll();
      await pollTick();
    } catch (e) {
      appendLog(`初始化失败: ${e.message}`);
    }
  })();
})();
