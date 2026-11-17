(function () {
  "use strict";

  document.querySelectorAll("form.upload-card").forEach(function (form) {
    form.addEventListener("submit", function () {
      form.querySelector(".feedback").textContent = "正在上传并解析合同，请稍候；随后进入审查进度页。";
      // Preserve the clicked submitter's formaction during native submission.
      window.setTimeout(function () {
        form.querySelectorAll('button[type="submit"]').forEach(function (button) {
          button.disabled = true;
        });
      }, 0);
    });
  });

  function openAncestors(node) {
    let details = node.closest("details");
    while (details) {
      details.open = true;
      details = details.parentElement ? details.parentElement.closest("details") : null;
    }
  }

  function setFieldError(form, name, message) {
    if (!name) return;
    const space = form.querySelector('[data-error-for="' + name.replace(/"/g, '\\"') + '"]');
    if (space) space.textContent = message || "";
  }

  function clearErrors(form) {
    form.querySelectorAll(".field-error").forEach(function (node) {
      node.textContent = "";
    });
  }

  function fieldByName(form, name) {
    if (!name) return null;
    if (typeof form.elements.namedItem === "function") {
      return form.elements.namedItem(name);
    }
    return null;
  }

  function focusField(form, name) {
    const field = fieldByName(form, name);
    if (!field) return;
    const holder = field.closest("details");
    if (holder) holder.open = true;
    field.focus();
  }

  function messageFrom(data, status) {
    if (data && typeof data.detail === "string") {
      return { message: data.detail.replaceAll("provided", "已提供"), field: "" };
    }
    if (data && data.detail && typeof data.detail === "object" && !Array.isArray(data.detail)) {
      return {
        message: String(data.detail.message || "提交内容需要修正。").replaceAll("provided", "已提供"),
        field: data.detail.field || "",
      };
    }
    if (data && Array.isArray(data.detail)) {
      const first = data.detail[0] || {};
      const loc = Array.isArray(first.loc) ? first.loc : [];
      return { message: "请补全或修正标出的字段。", field: loc.length ? loc[loc.length - 1] : "" };
    }
    const fallback = {
      400: "提交内容需要修正，请检查标出的字段。",
      403: "当前执行方式未获服务端允许，请重新选择。",
      404: "要打开的记录不存在。",
      409: "提交内容已改变，请重新打开确认页后再试。",
      413: "文件超过当前限制。",
      429: "系统正在处理其他任务，请稍后重试。",
      503: "本地存储暂不可用，请检查服务或稍后重试。"
    };
    return { message: fallback[status] || "提交未成功，请核对填写内容。", field: "" };
  }

  function syncMaterial(select) {
    const details = select.closest("details");
    if (!details) return;
    const file = details.querySelector('input[type="file"]');
    if (file) {
      file.required = select.value === "provided";
      file.disabled = select.value !== "provided";
      if (file.disabled) file.value = "";
    }
    const badge = details.querySelector("[data-material-badge]");
    if (badge) {
      const labels = {
        unknown: ["尚未核实", "pill-warn"],
        provided: ["已提供", "pill-ok"],
        missing: ["未提供", "pill-danger"],
        not_applicable: ["已声明不适用", "pill-neutral"]
      };
      const pair = labels[select.value] || ["待核对", "pill-neutral"];
      badge.textContent = pair[0];
      badge.className = "pill " + pair[1];
    }
  }

  document.querySelectorAll("select[data-material]").forEach(function (select) {
    syncMaterial(select);
    select.addEventListener("change", function () {
      syncMaterial(select);
    });
  });

  const startButton = document.querySelector("[data-start-button]");
  const startHint = document.querySelector("[data-start-hint]");
  const modeRadios = document.querySelectorAll('input[name="execution_mode"]');
  function syncStartLabel() {
    if (!startButton) return;
    const checked = document.querySelector('input[name="execution_mode"]:checked');
    const live = checked && checked.value === "LIVE";
    startButton.textContent = live ? "确认并开始真实审查" : "确认并开始流程演练";
    if (startHint) {
      startHint.textContent = live
        ? "真实模型结果只是候选，仍需人工核对；失败不会自动改用模拟。"
        : "流程演练不会分析你的合同，也不会产生业务判断。";
    }
  }
  modeRadios.forEach(function (radio) {
    radio.addEventListener("change", syncStartLabel);
  });
  syncStartLabel();

  document.querySelectorAll("form[data-ajax]").forEach(function (form) {
    form.addEventListener(
      "invalid",
      function (event) {
        const target = event.target;
        if (!target || !target.name) return;
        const holder = target.closest("details");
        if (holder) holder.open = true;
        const message =
          target.validity && target.validity.valueMissing
            ? "请填写此项。"
            : "请检查此字段。";
        setFieldError(form, target.name, message);
      },
      true
    );

    form.addEventListener("input", function (event) {
      if (event.target && event.target.name) setFieldError(form, event.target.name, "");
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const feedback = form.querySelector(".feedback");
      clearErrors(form);
      if (feedback) {
        feedback.textContent = "正在提交，请稍候…";
        feedback.removeAttribute("role");
      }
      if (button) button.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          redirect: "follow",
          headers: { Accept: "text/html,application/json" }
        });
        if (!response.ok) {
          let data = null;
          try {
            data = await response.json();
          } catch (ignored) {
            data = null;
          }
          const parsed = messageFrom(data, response.status);
          if (parsed.field) {
            setFieldError(form, parsed.field, parsed.message);
            focusField(form, parsed.field);
          }
          throw new Error(parsed.message);
        }
        if (response.redirected && response.url) {
          if (form.dataset.kind === "opinion") {
            sessionStorage.setItem("contract-feedback", "人工意见已保存，原结果保持不变。");
          }
          window.location.assign(response.url);
          return;
        }
        if (feedback) feedback.textContent = "已保存。";
        if (button) button.disabled = false;
      } catch (error) {
        if (feedback) {
          feedback.textContent = error.message || "提交未成功，请核对填写内容。";
          feedback.setAttribute("role", "alert");
          feedback.tabIndex = -1;
          feedback.focus();
        }
        if (button) button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-versions]").forEach(async function (box) {
    try {
      const response = await fetch("/api/contracts/" + box.dataset.versions + "/versions");
      if (!response.ok) throw new Error();
      const versions = await response.json();
      box.innerHTML = "";
      versions.forEach(function (version) {
        const link = document.createElement("a");
        link.href = "/select/" + version.task_id + "/" + version.version_id;
        link.textContent = "版本 " + version.version_id.slice(0, 12) + " · " + version.created_at;
        box.appendChild(link);
      });
    } catch (error) {
      box.textContent = "版本列表读取失败，请刷新重试。";
    }
  });

  document.querySelectorAll("[data-locator]").forEach(function (button) {
    button.addEventListener("click", function () {
      const target = document.getElementById(button.dataset.locator);
      if (!target) {
        button.textContent = "未找到完整来源";
        return;
      }
      openAncestors(target);
      target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      const mark = target.querySelector("mark");
      if (mark) {
        mark.classList.add("mark-focus");
        window.setTimeout(function () {
          mark.classList.remove("mark-focus");
        }, 2600);
      }
    });
  });

  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () {
      window.print();
    });
  });

  const selectedWorkbench = document.querySelector(".workbench-grid.has-selection");
  if (selectedWorkbench && window.matchMedia("(max-width: 960px)").matches) {
    const detail = document.getElementById("item-detail");
    if (detail) detail.scrollIntoView({ block: "start" });
  }

  const saved = sessionStorage.getItem("contract-feedback");
  if (saved) {
    sessionStorage.removeItem("contract-feedback");
    const main = document.querySelector("main");
    if (main && !document.querySelector(".flash")) {
      const notice = document.createElement("div");
      notice.className = "flash";
      notice.setAttribute("role", "status");
      notice.textContent = saved;
      main.prepend(notice);
    }
  }
})();
