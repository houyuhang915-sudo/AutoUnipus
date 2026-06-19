// AutoUnipus Web UI 前端逻辑
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------- 配置加载/保存 ----------

  let allHandlers = [];

  async function loadConfig() {
    const r = await fetch("/api/config");
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      msg("cfg-msg", e.error || "加载配置失败", "err");
      return;
    }
    const { config, all_handlers } = await r.json();
    allHandlers = all_handlers || [];
    renderHandlers(config.handlers || allHandlers);
    fillForm(config);
  }

  function renderHandlers(enabled) {
    const wrap = $("#handlers-list");
    wrap.innerHTML = "";
    const enabledSet = new Set(enabled);
    for (const name of allHandlers) {
      const id = `h-${name}`;
      const label = document.createElement("label");
      label.className = "check";
      label.innerHTML = `
        <input type="checkbox" data-handler="${name}" id="${id}" ${enabledSet.has(name) ? "checked" : ""}>
        <span>${name}</span>
      `;
      wrap.appendChild(label);
    }
  }

  function fillForm(cfg) {
    const f = $("#cfg-form");
    f.username.value = cfg.username || "";
    f.password.value = cfg.password || "";
    f.Driver.value = cfg.Driver || "Edge";
    f.Automode.value = cfg.Automode ? "true" : "false";
    f.class_url.value = (cfg.class_url || []).join("\n");
    f.dry_run.checked = !!cfg.dry_run;
    f.harvest_first_pass.checked = !!cfg.harvest_first_pass;
    f.reading_duration_sec.value = cfg.reading_duration_sec ?? 8;
    f["cache.enabled"].checked = cfg.cache?.enabled !== false;
    f["cache.path"].value = cfg.cache?.path || "data/answers.db";
    f["ai.enabled"].checked = !!cfg.ai?.enabled;
    f["ai.base_url"].value = cfg.ai?.base_url || "";
    f["ai.api_key"].value = cfg.ai?.api_key || "";
    f["ai.model"].value = cfg.ai?.model || "";
    f["ai.fallback_models"].value = (cfg.ai?.fallback_models || []).join("\n");
  }

  function gatherForm() {
    const f = $("#cfg-form");
    const enabledHandlers = Array.from(
      $$('#handlers-list input[type=checkbox]:checked')
    ).map((el) => el.dataset.handler);

    const urls = (f.class_url.value || "")
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);

    return {
      username: f.username.value.trim(),
      password: f.password.value,
      Automode: f.Automode.value === "true",
      Driver: f.Driver.value,
      class_url: urls,
      handlers: enabledHandlers,
      dry_run: !!f.dry_run.checked,
      harvest_first_pass: !!f.harvest_first_pass.checked,
      reading_duration_sec: parseInt(f.reading_duration_sec.value, 10) || 8,
      cache: {
        enabled: !!f["cache.enabled"].checked,
        path: f["cache.path"].value.trim() || "data/answers.db",
      },
      ai: {
        enabled: !!f["ai.enabled"].checked,
        base_url: f["ai.base_url"].value.trim(),
        api_key: f["ai.api_key"].value.trim(),
        model: f["ai.model"].value.trim() || "deepseek-chat",
        fallback_models: (f["ai.fallback_models"].value || "")
          .split(/\r?\n/)
          .map((s) => s.trim())
          .filter(Boolean),
        timeout: 30,
      },
      debug: false,
    };
  }

  $("#cfg-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = gatherForm();
    const r = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("cfg-msg", "已保存", "ok");
    } else {
      msg("cfg-msg", data.error || `HTTP ${r.status}`, "err");
    }
  });

  // ---------- 运行控制 ----------

  $("#btn-start").addEventListener("click", async () => {
    const body = gatherForm();
    // 启动前先把当前表单存盘,免得用户改了没保存就跑
    const saveR = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!saveR.ok) {
      const d = await saveR.json().catch(() => ({}));
      msg("run-msg", d.error || "保存配置失败", "err");
      return;
    }
    const mode = body.Automode ? "auto" : "assist";
    const r = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("run-msg", `已启动 (${mode})`, "ok");
    } else {
      msg("run-msg", data.error || `HTTP ${r.status}`, "err");
    }
  });

  $("#btn-stop").addEventListener("click", async () => {
    const r = await fetch("/api/stop", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    msg("run-msg", r.ok ? "已请求停止(关闭浏览器窗口生效更快)" : (d.error || "停止失败"), r.ok ? "ok" : "err");
  });

  $("#btn-trigger").addEventListener("click", async () => {
    const r = await fetch("/api/trigger", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    msg("run-msg", r.ok ? "已触发一次扫描" : (d.error || "触发失败"), r.ok ? "ok" : "err");
  });

  // ---------- 状态轮询 ----------

  async function pollState() {
    try {
      const r = await fetch("/api/state");
      const s = await r.json();
      const dot = $("#status-dot");
      const txt = $("#status-text");
      dot.classList.remove("running", "error");
      if (s.running) {
        dot.classList.add("running");
        const elapsed = s.elapsed != null ? Math.floor(s.elapsed) : 0;
        txt.textContent = `运行中 (${s.mode})  ${elapsed}s`;
      } else if (s.last_error) {
        dot.classList.add("error");
        txt.textContent = `空闲  上次错误: ${s.last_error}`;
      } else {
        txt.textContent = "空闲";
      }
    } catch (_) {
      $("#status-text").textContent = "无法连接服务";
    }
  }
  setInterval(pollState, 1000);
  pollState();

  // ---------- 日志 SSE ----------

  const logsEl = $("#logs");
  const seenLines = new Set();
  let evt = null;

  function appendLog(entry) {
    // 简单去重避免重复:把 ts+msg 当 key
    const k = `${entry.ts}|${entry.msg}`;
    if (seenLines.has(k)) return;
    seenLines.add(k);
    if (seenLines.size > 5000) {
      // 清一些防内存膨胀
      const arr = Array.from(seenLines).slice(-2500);
      seenLines.clear();
      arr.forEach((x) => seenLines.add(x));
    }
    const div = document.createElement("span");
    div.className = `logline ${entry.level || "info"}`;
    div.textContent = entry.msg + "\n";
    logsEl.appendChild(div);
    if ($("#autoscroll").checked) {
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  }

  function connectLogs() {
    if (evt) evt.close();
    evt = new EventSource("/api/logs/stream");
    evt.onmessage = (e) => {
      try {
        const entry = JSON.parse(e.data);
        appendLog(entry);
      } catch (_) {}
    };
    evt.onerror = () => {
      // 浏览器会自动重连;这里可加 UI 提示
    };
  }
  connectLogs();

  $("#btn-clear-logs").addEventListener("click", async () => {
    await fetch("/api/logs/clear", { method: "POST" });
    seenLines.clear();
    logsEl.innerHTML = "";
  });

  // ---------- 缓存 ----------

  async function loadCache() {
    const el = $("#cache-info");
    try {
      const r = await fetch("/api/cache/stats");
      const s = await r.json();
      if (s.error) {
        el.textContent = "错误: " + s.error;
        return;
      }
      el.textContent =
        `路径: ${s.path}\n` +
        `条目: ${s.rows ?? 0}\n` +
        `大小: ${formatBytes(s.size_bytes ?? 0)}`;
    } catch (e) {
      el.textContent = "读取失败: " + e.message;
    }
  }

  $("#btn-refresh-cache").addEventListener("click", loadCache);
  $("#btn-clear-cache").addEventListener("click", async () => {
    if (!confirm("确定要清空所有答案缓存?")) return;
    const r = await fetch("/api/cache", { method: "DELETE" });
    if (r.ok) loadCache();
  });

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }

  // ---------- 手动答案库 ----------

  async function loadManual() {
    try {
      const r = await fetch("/api/manual-answers");
      const d = await r.json();
      $("#manual-json").value = d.raw || "{}";
    } catch (e) {
      msg("manual-msg", "读取失败: " + e.message, "err");
    }
  }

  $("#btn-save-manual").addEventListener("click", async () => {
    const raw = $("#manual-json").value;
    const r = await fetch("/api/manual-answers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("manual-msg", "已保存(下一题生效)", "ok");
    } else {
      msg("manual-msg", d.error || `HTTP ${r.status}`, "err");
    }
  });

  $("#btn-reload-manual").addEventListener("click", loadManual);

  // ---------- 快速录入答案 ----------

  $("#btn-quick-add").addEventListener("click", async () => {
    const kind = $("#quick-kind").value;
    const key = $("#quick-key").value.trim();
    const raw = $("#quick-answers").value.trim();
    const answers = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!key) {
      msg("quick-msg", "key 不能为空", "err");
      return;
    }
    if (answers.length === 0) {
      msg("quick-msg", "至少要填一行答案", "err");
      return;
    }
    const r = await fetch("/api/manual-answers/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, key, answers }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("quick-msg", `已添加 ${d.count} 条到 ${d.section}.${d.key}`, "ok");
      $("#quick-key").value = "";
      $("#quick-answers").value = "";
      loadManual();  // 刷新下面的 JSON 预览
    } else {
      msg("quick-msg", d.error || `HTTP ${r.status}`, "err");
    }
  });

  // ---------- 批量导入 ----------

  async function bulkParse() {
    const raw = $("#bulk-raw").value;
    const r = await fetch("/api/manual-answers/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg("bulk-msg", d.error || `HTTP ${r.status}`, "err");
      return null;
    }
    return d.blocks || [];
  }

  $("#btn-bulk-preview").addEventListener("click", async () => {
    const blocks = await bulkParse();
    if (!blocks) return;
    const el = $("#bulk-preview");
    if (blocks.length === 0) {
      el.style.display = "block";
      el.textContent = "(没解析出任何块。检查格式:用空行分块,每块第一行是 key)";
      return;
    }
    el.style.display = "block";
    el.innerHTML = blocks
      .map((b) => `<strong>${escapeHtml(b.key)}</strong> (${b.count} 条):\n  ${b.answers.map(escapeHtml).join(" | ")}`)
      .join("\n\n");
    msg("bulk-msg", `解析出 ${blocks.length} 个块,确认无误后点"直接导入"`, "ok");
  });

  $("#btn-bulk-import").addEventListener("click", async () => {
    const blocks = await bulkParse();
    if (!blocks) return;
    if (blocks.length === 0) {
      msg("bulk-msg", "没解析出任何块", "err");
      return;
    }
    if (!confirm(`确认导入 ${blocks.length} 个块到答案库?同名 key 会被覆盖。`)) return;
    const kind = $("#bulk-kind").value;
    const r = await fetch("/api/manual-answers/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, blocks }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("bulk-msg", `成功导入 ${d.added} 个块到 ${d.section}`, "ok");
      $("#bulk-raw").value = "";
      $("#bulk-preview").style.display = "none";
      loadManual();
    } else {
      msg("bulk-msg", d.error || `HTTP ${r.status}`, "err");
    }
  });

  // ---------- 从 SQLite 缓存补回 manual_answers.json ----------
  $("#btn-sync-cache").addEventListener("click", async () => {
    if (!confirm("把 data/answers.db 里所有题答案补回 manual_answers.json 的 exact_by_task?\n(只补缺,不会覆盖你已经手填的精修答案)")) return;
    msg("sync-cache-msg", "同步中…", "");
    const r = await fetch("/api/manual-answers/sync-from-cache", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("sync-cache-msg",
        `成功补回 ${d.added} 个 task,跳过 ${d.skipped}${d.note ? ` (${d.note})` : ""}`,
        "ok");
      loadManual();
    } else {
      msg("sync-cache-msg", d.error || `HTTP ${r.status}`, "err");
    }
  });

  // ---------- 删掉某题的脏数据 ----------
  $("#btn-forget-task").addEventListener("click", async () => {
    const tid = ($("#forget-task-id").value || "").trim();
    if (!tid) {
      msg("forget-task-msg", "请先填 task_id", "err");
      return;
    }
    if (!confirm(`确认删除 ${tid} 的 SQLite 缓存条目 + manual_answers.json 里的 exact_by_task.${tid} ?`)) return;
    const r = await fetch("/api/manual-answers/forget-task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: tid }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      msg("forget-task-msg",
        `${tid}: 缓存删 ${d.cache_deleted} 行,manual ${d.manual_deleted ? "已删" : "未命中"}`,
        "ok");
      $("#forget-task-id").value = "";
      loadManual();
    } else {
      msg("forget-task-msg", d.error || `HTTP ${r.status}`, "err");
    }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // ---------- AI 连通测试 ----------

  $("#btn-test-ai").addEventListener("click", async () => {
    // 先把当前表单的 AI 设置存盘,免得用户改了没保存
    const body = gatherForm();
    const saveR = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!saveR.ok) {
      const d = await saveR.json().catch(() => ({}));
      msg("ai-test-msg", "保存配置失败: " + (d.error || saveR.status), "err");
      return;
    }
    msg("ai-test-msg", "测试中...", "");
    const r = await fetch("/api/ai/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const d = await r.json().catch(() => ({}));
    const detail = $("#ai-test-detail");
    detail.style.display = "block";
    detail.textContent = JSON.stringify(d, null, 2);
    if (r.ok && d.ok) {
      msg(
        "ai-test-msg",
        `✓ 连通成功 (${d.elapsed_ms}ms)  reply=${JSON.stringify(d.reply)}`,
        "ok"
      );
    } else {
      msg(
        "ai-test-msg",
        `✗ 连通失败: ${d.error || ("HTTP " + r.status)}`,
        "err"
      );
    }
  });

  // ---------- 工具 ----------

  function msg(id, text, kind) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = `msg ${kind || ""}`;
    if (kind === "ok") {
      setTimeout(() => {
        if (el.textContent === text) el.textContent = "";
      }, 2500);
    }
  }

  // ---------- 初始化 ----------

  loadConfig();
  loadCache();
  loadManual();
})();
