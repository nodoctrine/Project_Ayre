/* Ayre-UI · workspace.js — the Workspace / Tools surfaces.
   Load order: after setup.js.  CONSUMES Ayre.{root,esc,el,getJSON,textEl,BRIDGE_DOWN}.
   EXPOSES: nothing. Talks to other surfaces only through document events
   (ayre:project-changed, ayre:tools-changed).
   Contents:
     - file manager      workspace file browser (collapsible sections)
     - tools nav submenu  active-project files listed in the rail
     - project selector   chat-header project dropdown
     - project manager    create / rename / delete projects (Tools tab)
     - tool panel         per-tool enable toggles
     - RAG library        corpus index status (the master toggles live in Settings)
     - Skill Builder      author custom skills. SECURITY: the injection gating is
                          server-side (Devlog #10); this UI only POSTs to /api/skills.
   Split from app.js 2026-07-05. */
(function () {
  'use strict';
  var Ayre = window.Ayre;
  var root = Ayre.root, BRIDGE_DOWN = Ayre.BRIDGE_DOWN,
      el = Ayre.el, esc = Ayre.esc, getJSON = Ayre.getJSON, textEl = Ayre.textEl;
  /* ── Tools: workspace file manager ── */
  (function wireCollapsibles() {
    document.querySelectorAll('#view-tools .sec-head.collapsible').forEach(function (head) {
      var body = document.getElementById(head.dataset.target);
      if (!body) return;
      head.addEventListener('click', function () {
        var nowCollapsed = head.classList.toggle('collapsed');
        body.hidden = nowCollapsed;
      });
    });
  })();

  /* ── Tools nav submenu: active project files in the rail ── */
  (function wireToolsSubmenu() {
    var toggle = document.getElementById('toolsSubToggle');
    var submenu = document.getElementById('toolsSubmenu');
    if (!toggle || !submenu) return;

    var open = false;

    function setOpen(v) {
      open = v;
      submenu.hidden = !open;
      toggle.classList.toggle('open', open);
    }

    function load() {
      fetch('/api/projects').then(function (r) { return r.json(); }).then(function (data) {
        submenu.innerHTML = '';
        var proj = (data.projects || []).find(function (p) { return p.name === data.active; });
        var files = (proj && proj.files) || [];
        if (!files.length) {
          submenu.appendChild(el('div', 'nav-sub-empty', 'No files'));
        } else {
          files.forEach(function (f) {
            var row = document.createElement('button');
            row.className = 'nav-sub-file';
            row.textContent = f.name;
            row.title = f.name;
            submenu.appendChild(row);
          });
        }
      }).catch(function () {
        submenu.innerHTML = '';
        submenu.appendChild(el('div', 'nav-sub-empty', '—'));
      });
    }

    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      setOpen(!open);
      if (open) load();
    });

    document.addEventListener('ayre:project-changed', function () {
      if (open) load();
    });
  })();

  /* ── Project selector (chat header) ── */
  (function wireProjectSelector() {
    var wrap = document.getElementById('projectSelect');
    var btn = document.getElementById('projectBtn');
    var dropdown = document.getElementById('projectDropdown');
    var listEl = document.getElementById('projectDropdownList');
    var newBtn = document.getElementById('projectDropdownNew');
    var newForm = document.getElementById('projectNewForm');
    var newInput = document.getElementById('projectNewInput');
    var newCreate = document.getElementById('projectNewCreate');
    var newCancel = document.getElementById('projectNewCancel');
    var newMsg = document.getElementById('projectNewMsg');
    if (!btn) return;

    var activeProject = '';

    function load() {
      getJSON('/api/projects').then(function (data) {
        activeProject = data.active || 'Default';
        btn.textContent = 'Project: ' + activeProject + ' ▾';
        renderList(data.projects || [], activeProject);
      }).catch(function () { btn.textContent = 'Project: —'; });
    }

    function renderList(projects, active) {
      listEl.innerHTML = '';
      projects.forEach(function (p) {
        var item = el('button', 'proj-drop-item' + (p.name === active ? ' proj-drop-active' : ''));
        item.textContent = p.name + (p.name === active ? ' ✓' : '');
        item.addEventListener('click', function () { switchProject(p.name); });
        listEl.appendChild(item);
      });
    }

    function switchProject(name) {
      fetch('/api/projects/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
          activeProject = d.active;
          btn.textContent = 'Project: ' + activeProject + ' ▾';
          dropdown.hidden = true;
          document.dispatchEvent(new CustomEvent('ayre:project-changed', { detail: { active: activeProject } }));
          load();
        }
      });
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var opening = dropdown.hidden;
      dropdown.hidden = !opening;
      if (opening) { newForm.hidden = true; newBtn.hidden = false; newMsg.hidden = true; }
    });
    document.addEventListener('click', function (e) {
      if (wrap && !wrap.contains(e.target)) dropdown.hidden = true;
    });

    newBtn.addEventListener('click', function () {
      newForm.hidden = false; newBtn.hidden = true; newMsg.hidden = true;
      newInput.value = ''; newInput.focus();
    });
    newCancel.addEventListener('click', function () {
      newForm.hidden = true; newBtn.hidden = false; newMsg.hidden = true;
    });
    newCreate.addEventListener('click', function () {
      var name = newInput.value.trim();
      if (!name) return;
      fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
          newForm.hidden = true; newBtn.hidden = false; newMsg.hidden = true;
          switchProject(d.name);
        } else {
          newMsg.textContent = d.error || 'Could not create project.';
          newMsg.hidden = false;
        }
      });
    });
    newInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') newCreate.click();
      if (e.key === 'Escape') newCancel.click();
    });

    document.addEventListener('ayre:nav', function (e) { if (e.detail.view === 'chat') load(); });
    load();
    window._reloadProjectSelector = load;
  })();

  /* ── Project manager (Tools tab) ── */
  (function wireProjectManager() {
    var host = document.getElementById('projectManager');
    if (!host) return;

    function fmtSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function load() {
      host.innerHTML = '<div class="muted">Loading…</div>';
      getJSON('/api/projects').then(function (data) {
        render(data.projects || [], data.active || 'Default');
      }).catch(function () {
        host.innerHTML = '<div class="hint">Could not load projects. Is the bridge running?</div>';
      });
    }

    function render(projects, active) {
      host.innerHTML = '';

      projects.forEach(function (proj) {
        var card = el('div', 'proj-card' + (proj.name === active ? ' proj-card-active' : ''));

        var head = el('div', 'proj-card-head');
        head.appendChild(el('span', 'proj-card-name', esc(proj.name)));
        if (proj.name === active) head.appendChild(el('span', 'proj-card-badge', 'active'));
        head.appendChild(el('span', 'proj-card-count', proj.file_count + ' file' + (proj.file_count === 1 ? '' : 's')));
        if (proj.name !== active) {
          var setBtn = el('button', 'proj-card-set secondary', 'Set active');
          setBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            fetch('/api/projects/active', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: proj.name }),
            }).then(function (r) { return r.json(); }).then(function (d) {
              if (d.ok) {
                document.dispatchEvent(new CustomEvent('ayre:project-changed', { detail: { active: d.active } }));
                if (window._reloadProjectSelector) window._reloadProjectSelector();
                load();
              }
            });
          });
          head.appendChild(setBtn);
        }
        var chevron = el('span', 'proj-card-chevron', '▾');
        head.appendChild(chevron);
        card.appendChild(head);

        var body = el('div', 'proj-card-body');
        body.hidden = true;
        buildProjectBody(body, proj);
        card.appendChild(body);

        head.addEventListener('click', function () {
          body.hidden = !body.hidden;
          chevron.textContent = body.hidden ? '▾' : '▴';
        });
        host.appendChild(card);
      });

      // New project form
      var newWrap = el('div', 'proj-new-wrap');
      var newBtn = el('button', 'secondary proj-new-btn', '+ New project');
      var newForm = el('div', 'proj-new-form');
      newForm.hidden = true;
      var newInput = document.createElement('input');
      newInput.type = 'text'; newInput.placeholder = 'Project name'; newInput.maxLength = 64;
      newInput.className = 'proj-new-input';
      var newConfirm = el('button', '', 'Create');
      var newCancel = el('button', 'secondary', 'Cancel');
      var newMsg = el('div', 'proj-new-msg'); newMsg.hidden = true;
      newForm.appendChild(newInput); newForm.appendChild(newConfirm);
      newForm.appendChild(newCancel); newForm.appendChild(newMsg);
      newWrap.appendChild(newBtn); newWrap.appendChild(newForm);

      newBtn.addEventListener('click', function () {
        newForm.hidden = false; newBtn.hidden = true; newMsg.hidden = true; newInput.focus();
      });
      newCancel.addEventListener('click', function () {
        newForm.hidden = true; newBtn.hidden = false; newMsg.hidden = true;
      });
      newConfirm.addEventListener('click', function () {
        var name = newInput.value.trim();
        if (!name) return;
        fetch('/api/projects', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name }),
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.ok) {
            newForm.hidden = true; newBtn.hidden = false; newMsg.hidden = true;
            if (window._reloadProjectSelector) window._reloadProjectSelector();
            load();
          } else {
            newMsg.textContent = d.error || 'Could not create project.';
            newMsg.hidden = false;
          }
        });
      });
      newInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') newConfirm.click();
        if (e.key === 'Escape') newCancel.click();
      });
      host.appendChild(newWrap);
    }

    function buildProjectBody(body, proj) {
      // File list
      if (proj.files && proj.files.length) {
        proj.files.forEach(function (f) {
          appendFileRow(body, f, proj.name);
        });
      } else {
        body.appendChild(el('div', 'muted proj-empty', 'No files yet.'));
      }

      // Upload zone
      var zone = el('div', 'upload-zone proj-upload');
      var label = el('label', 'upload-label', 'browse');
      var fileInput = document.createElement('input');
      fileInput.type = 'file'; fileInput.multiple = true;
      label.appendChild(fileInput);
      var span = document.createElement('span');
      span.appendChild(document.createTextNode('Drop files here, or '));
      span.appendChild(label);
      zone.appendChild(span);

      var uploadMsg = el('div', 'upload-msg'); uploadMsg.hidden = true;
      function setMsg(text, cls) {
        uploadMsg.textContent = text || '';
        uploadMsg.className = 'upload-msg' + (cls ? ' ' + cls : '');
        uploadMsg.hidden = !text;
      }

      function uploadFile(file) {
        return new Promise(function (resolve) {
          var reader = new FileReader();
          reader.onload = function (e) {
            var bytes = new Uint8Array(e.target.result);
            var chunks = [], CHUNK = 0x8000;
            for (var i = 0; i < bytes.length; i += CHUNK) {
              chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
            }
            var b64 = btoa(chunks.join(''));
            fetch('/api/workspace/upload', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: file.name, content_b64: b64, project: proj.name }),
            }).then(function (r) { return r.json(); }).then(function (res) {
              if (!res.ok) setMsg('Upload failed: ' + (res.error || 'unknown'), 'err');
              resolve();
            }).catch(function () { setMsg('Upload failed: bridge unreachable.', 'err'); resolve(); });
          };
          reader.readAsArrayBuffer(file);
        });
      }

      function uploadFiles(files) {
        if (!files || !files.length) return;
        setMsg('Uploading…', '');
        var chain = Promise.resolve();
        Array.from(files).forEach(function (f) { chain = chain.then(function () { return uploadFile(f); }); });
        chain.then(function () {
          if (!uploadMsg.classList.contains('err')) setMsg('Done.', 'ok');
          setTimeout(function () { setMsg(''); }, 2500);
          load();
        });
      }

      fileInput.addEventListener('change', function () { uploadFiles(fileInput.files); fileInput.value = ''; });
      zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('drag-over'); });
      zone.addEventListener('dragleave', function () { zone.classList.remove('drag-over'); });
      zone.addEventListener('drop', function (e) {
        e.preventDefault(); zone.classList.remove('drag-over'); uploadFiles(e.dataTransfer.files);
      });
      body.appendChild(zone);
      body.appendChild(uploadMsg);
    }

    function appendFileRow(body, f, projName) {
      var row = el('div', 'wfile');
      var info = el('div', 'wfile-info');
      info.appendChild(el('span', 'wfile-name', esc(f.name)));
      info.appendChild(el('span', 'wfile-meta', esc(fmtSize(f.size))));
      var delBtn = el('button', 'wfile-del', '×');
      delBtn.title = 'Delete ' + f.name;

      // Inline confirm: clicking × expands a confirm row; second click deletes.
      var confirmRow = el('div', 'wfile-confirm');
      confirmRow.hidden = true;
      confirmRow.appendChild(el('span', 'wfile-confirm-label', '⚠ Delete ' + f.name + '? This cannot be undone.'));
      var confirmYes = el('button', 'wfile-confirm-yes', 'Delete');
      var confirmNo = el('button', 'wfile-confirm-no secondary', 'Cancel');
      confirmRow.appendChild(confirmYes);
      confirmRow.appendChild(confirmNo);

      delBtn.addEventListener('click', function () {
        confirmRow.hidden = false;
        delBtn.disabled = true;
      });
      confirmNo.addEventListener('click', function () {
        confirmRow.hidden = true;
        delBtn.disabled = false;
      });
      confirmYes.addEventListener('click', function () {
        fetch('/api/workspace/file', {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: f.name, project: projName }),
        }).then(function (r) { return r.json(); }).then(function (d) { if (d.ok) load(); });
      });

      row.appendChild(info);
      row.appendChild(delBtn);
      body.appendChild(row);
      body.appendChild(confirmRow);
    }

    document.addEventListener('ayre:nav', function (e) { if (e.detail.view === 'tools') load(); });
    document.addEventListener('ayre:project-changed', function () { load(); });
    load();
  })();

  /* ── Tool panel (Tools tab: toggle list) ── */
  (function wireToolPanel() {
    var listEl = document.getElementById('toolList');
    if (!listEl) return;

    function renderTools(tools) {
      listEl.innerHTML = '';
      if (!tools || !tools.length) {
        listEl.innerHTML = '<div class="muted">No tools available.</div>';
        return;
      }
      tools.forEach(function (t) {
        var card = el('div', 'tool-card');
        var head = el('div', 'tool-card-head');
        head.appendChild(el('span', 'tool-card-label', esc(t.label)));
        var tog = el('button', 'tool-toggle' + (t.enabled ? ' on' : ''), t.enabled ? 'on' : 'off');
        head.appendChild(tog);
        card.appendChild(head);
        card.appendChild(el('p', 'tool-card-desc', esc(t.description)));

        // write_file carries a second control: the confirmation gate (Allow/Deny before
        // each write). PLACEHOLDER copy — pending the user-authored pass.
        if (t.name === 'write_file' && typeof t.confirm === 'boolean') {
          var crow = el('div', 'tool-subtoggle');
          crow.appendChild(el('span', 'tool-subtoggle-label', 'Ask before writing files'));
          var ctog = el('button', 'tool-toggle' + (t.confirm ? ' on' : ''), t.confirm ? 'on' : 'off');
          crow.appendChild(ctog);
          card.appendChild(crow);
          ctog.addEventListener('click', function () {
            ctog.disabled = true;
            fetch('/api/tools/write-confirm', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ enabled: !t.confirm })
            }).then(function (r) { return r.json(); }).then(function (d) {
              if (d.ok) renderTools(d.tools); else ctog.disabled = false;
            }).catch(function () { ctog.disabled = false; });
          });
        }

        var warn = el('div', 'tool-warn');
        warn.hidden = true;
        warn.appendChild(el('p', 'tool-warn-text', '⚠ ' + esc(t.warning)));
        var wbtns = el('div', 'tool-warn-btns');
        var wdis = el('button', 'tool-warn-disable', 'Disable');
        var wcancel = el('button', 'tool-warn-cancel', 'Keep enabled');
        wbtns.appendChild(wdis);
        wbtns.appendChild(wcancel);
        warn.appendChild(wbtns);
        card.appendChild(warn);

        tog.addEventListener('click', function () {
          if (!t.enabled) {
            fetch('/api/tools/toggle', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: t.name, enabled: true })
            }).then(function (r) { return r.json(); }).then(function (d) {
              if (d.ok) { renderTools(d.tools); document.dispatchEvent(new CustomEvent('ayre:tools-changed')); }
            }).catch(function () {});
          } else {
            warn.hidden = !warn.hidden;
          }
        });

        wdis.addEventListener('click', function () {
          fetch('/api/tools/toggle', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: t.name, enabled: false })
          }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.ok) { renderTools(d.tools); document.dispatchEvent(new CustomEvent('ayre:tools-changed')); }
          }).catch(function () {});
        });

        wcancel.addEventListener('click', function () { warn.hidden = true; });
        listEl.appendChild(card);
      });
    }

    function loadToolPanel() {
      listEl.innerHTML = '<div class="muted">Loading…</div>';
      getJSON('/api/tools').then(function (d) { renderTools(d.tools || []); })
        .catch(function () { listEl.innerHTML = '<div class="muted">Could not load tools.</div>'; });
    }

    document.addEventListener('ayre:nav', function (e) {
      if (e.detail.view === 'tools') { loadToolPanel(); if (window.loadRagPanel) window.loadRagPanel(); }
    });

    loadToolPanel();
  })();

  /* ── RAG: master toggles live in Settings → Retrieval; the corpus list lives in
       Workspace → RAG library. Both surfaces share /api/rag state and re-render
       together on any toggle so they never drift. ── */
  (function wireRag() {
    var settingsEl = document.getElementById('ragSettings'); // Settings → Retrieval
    var corpusEl = document.getElementById('ragPanel');      // Workspace → RAG library
    if (!settingsEl && !corpusEl) return;

    function toggleBtn(on) {
      return el('button', 'tool-toggle' + (on ? ' on' : ''), on ? 'on' : 'off');
    }

    function post(key, enabled) {
      return fetch('/api/rag/toggle', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key, enabled: enabled })
      }).then(function (r) { return r.json(); });
    }

    // Wire a toggle button: POST `key`, then re-render BOTH surfaces on success.
    function wireToggle(btn, key, current) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return;
        btn.disabled = true;
        post(key, !current).then(function (d) {
          if (d.ok) renderAll(d); else btn.disabled = false;
        }).catch(function () { btn.disabled = false; });
      });
    }

    // Settings → Retrieval: the master on/off + the show-context preview toggle.
    function renderSettings(st) {
      if (!settingsEl) return;
      settingsEl.innerHTML = '';
      if (!st || !st.available) {
        settingsEl.appendChild(textEl('div', 'muted', 'Retrieval is unavailable on this install.'));
        return;
      }
      // Master switch. Disabled (with a hint) until a corpus index exists — turning it
      // on with no index would just be a silent no-op every turn.
      var row1 = el('div', 'rag-toggle-row');
      row1.appendChild(textEl('span', 'rag-toggle-label', 'Use retrieval in chat'));
      var t1 = toggleBtn(st.enabled);
      row1.appendChild(t1);
      settingsEl.appendChild(row1);
      if (!st.ready) {
        t1.disabled = true;
        t1.title = 'Build a corpus index first (Workspace → RAG library)';
        settingsEl.appendChild(textEl('p', 'subnote',
          'No corpus index found yet — build one, then turn this on.'));
      } else {
        wireToggle(t1, 'enabled', st.enabled);
      }

      // Show retrieved context (the raw injected passages preview under a reply).
      var row2 = el('div', 'rag-toggle-row');
      row2.appendChild(textEl('span', 'rag-toggle-label', 'Show retrieved context under replies'));
      var t2 = toggleBtn(st.show_retrieved_context);
      row2.appendChild(t2);
      settingsEl.appendChild(row2);
      wireToggle(t2, 'show_retrieved_context', st.show_retrieved_context);
    }

    // Workspace → RAG library: the corpus/corpora available to search. v0 ships one
    // corpus; per-corpus selection will render here when a second corpus is added.
    function renderCorpus(st) {
      if (!corpusEl) return;
      corpusEl.innerHTML = '';
      if (!st || !st.available) {
        corpusEl.appendChild(textEl('div', 'muted', 'Retrieval is unavailable on this install.'));
        return;
      }
      var status = el('div', 'rag-status');
      if (st.ready) {
        var c = (st.chunk_count || 0).toLocaleString('en-US');
        var a = st.article_count ? st.article_count.toLocaleString('en-US') + ' articles · ' : '';
        status.appendChild(textEl('span', 'rag-status-ok', '✓ Index ready'));
        status.appendChild(textEl('span', 'rag-status-detail',
          (st.corpus_label || 'Corpus') + ' — ' + a + c + ' passages'));
      } else {
        status.appendChild(textEl('span', 'rag-status-none', '○ No index found'));
        status.appendChild(textEl('span', 'rag-status-detail',
          st.error ? ('build one to enable retrieval (' + st.error + ')') : 'build one to enable retrieval'));
      }
      corpusEl.appendChild(status);
      corpusEl.appendChild(textEl('p', 'subnote',
        'One corpus ships in this build. Turn retrieval on or off in Settings → Retrieval.'));
    }

    function renderAll(st) { renderSettings(st); renderCorpus(st); }

    window.loadRagPanel = function () {
      if (corpusEl) corpusEl.innerHTML = '<div class="muted">Loading…</div>';
      if (settingsEl) settingsEl.innerHTML = '<div class="muted">Loading…</div>';
      getJSON('/api/rag').then(renderAll).catch(function () {
        var msg = '<div class="muted">Could not load retrieval status.</div>';
        if (corpusEl) corpusEl.innerHTML = msg;
        if (settingsEl) settingsEl.innerHTML = msg;
      });
    };

    // Refresh when the user opens Settings (the Tools view already refreshes via nav).
    document.addEventListener('ayre:nav', function (e) {
      if (e.detail && e.detail.view === 'settings') window.loadRagPanel();
    });

    window.loadRagPanel();
  })();

  /* ── Skill Builder (Workspace tab) ── */
  (function wireSkillBuilder() {
    var listEl = document.getElementById('skillList');
    var newBtn = document.getElementById('skillNewBtn');
    var builder = document.getElementById('skillBuilder');
    var titleInput = document.getElementById('skillTitle');
    var descInput = document.getElementById('skillDesc');
    var workflowInput = document.getElementById('skillWorkflow');
    var saveBtn = document.getElementById('skillSaveBtn');
    var cancelBtn = document.getElementById('skillCancelBtn');
    var titleCountEl = document.getElementById('skillTitleCount');
    var descCountEl = document.getElementById('skillDescCount');
    var msgEl = document.getElementById('skillMsg');
    if (!listEl || !newBtn || !builder) return;

    var editingId = null;  // null = creating new; string = editing existing

    function wordCount(s) {
      return s.trim().split(/\s+/).filter(Boolean).length;
    }

    function updateCounts() {
      var tw = titleInput ? wordCount(titleInput.value) : 0;
      var dw = descInput ? wordCount(descInput.value) : 0;
      if (titleCountEl) {
        titleCountEl.textContent = tw + ' / 5 words';
        titleCountEl.className = 'skill-limit' + (tw > 5 ? ' over' : '');
      }
      if (descCountEl) {
        descCountEl.textContent = dw + ' / 30 words';
        descCountEl.className = 'skill-limit' + (dw > 30 ? ' over' : '');
      }
    }

    function clearMsg() { if (msgEl) { msgEl.textContent = ''; msgEl.className = 'skill-msg'; } }
    function showErr(t) { if (msgEl) { msgEl.textContent = t; msgEl.className = 'skill-msg err'; } }

    function showBuilder(skill) {
      editingId = skill ? skill.id : null;
      if (titleInput) titleInput.value = skill ? skill.title : '';
      if (descInput) descInput.value = skill ? skill.description : '';
      if (workflowInput) workflowInput.value = skill ? skill.workflow : '';
      clearMsg();
      updateCounts();
      builder.hidden = false;
      newBtn.hidden = true;
      if (titleInput) titleInput.focus();
    }

    function hideBuilder() {
      builder.hidden = true;
      newBtn.hidden = false;
      editingId = null;
      clearMsg();
    }

    newBtn.addEventListener('click', function () { showBuilder(null); });
    if (cancelBtn) cancelBtn.addEventListener('click', hideBuilder);

    // "Make your own" on the built-in Handoff card: prefill the builder with an editable
    // Session-Handoff template, saved as the user's OWN custom skill (id:null => create).
    // A message-invoked skill cannot call save_handoff (that stays button-gated), so this
    // template only DRAFTS the note in chat and reminds the user to press Handoff → to save.
    // Draft copy — final voice comes in the whole-project Prose Pass.
    var HANDOFF_TEMPLATE = {
      id: null,
      title: 'Session Handoff',
      description: 'Drafts a structured end-of-session summary in the chat for you to review, then reminds you to press Handoff → to save it.',
      workflow: [
        'First, check whether this conversation contains substantive work. If there is nothing meaningful to summarise yet (an empty or trivial session), say so in one line and stop — do not invent a summary.',
        '',
        'Otherwise, look only at what is written in this chat window. Do not read any files, look for a previous handoff, or repeat anything already in memory.',
        '',
        'Write a session handoff note under four headings:',
        '1. What we worked on — the focus of this session.',
        '2. Decisions & preferences — anything decided, and preferences I expressed.',
        '3. Current state — what is done and working now, and anything unfinished.',
        '4. Next steps — the open items to pick up next session.',
        '',
        'Present the note in the chat for me to review. You cannot save it yourself — after I have read it, remind me to press the Handoff → button in the top bar to save it to my project folder (or to copy the text).'
      ].join('\n')
    };
    var customizeBtn = document.getElementById('handoffCustomizeBtn');
    if (customizeBtn) customizeBtn.addEventListener('click', function () { showBuilder(HANDOFF_TEMPLATE); });
    if (titleInput) titleInput.addEventListener('input', updateCounts);
    if (descInput) descInput.addEventListener('input', updateCounts);

    if (saveBtn) saveBtn.addEventListener('click', function () {
      var title = titleInput ? titleInput.value.trim() : '';
      var desc = descInput ? descInput.value.trim() : '';
      var workflow = workflowInput ? workflowInput.value.trim() : '';

      if (!title) { showErr('Title is required.'); return; }
      if (wordCount(title) > 5) { showErr('Title must be 5 words or fewer.'); return; }
      if (!desc) { showErr('Description is required.'); return; }
      if (wordCount(desc) > 30) { showErr('Description must be 30 words or fewer.'); return; }
      if (!workflow) { showErr('Workflow is required.'); return; }

      saveBtn.disabled = true;
      clearMsg();
      fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: editingId, title: title, description: desc, workflow: workflow }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        saveBtn.disabled = false;
        if (d.ok) { hideBuilder(); renderSkills(d.skills); }
        else { showErr(d.error || 'Could not save skill.'); }
      }).catch(function () { saveBtn.disabled = false; showErr(BRIDGE_DOWN); });
    });

    function renderSkills(skills) {
      // Remove any existing custom cards (keep the built-in Handoff card)
      var existing = listEl.querySelectorAll('.skill-card-custom');
      existing.forEach(function (c) { c.remove(); });

      (skills || []).forEach(function (skill) {
        var card = el('div', 'skill-card skill-card-custom');

        var head = el('div', 'skill-card-head');
        head.appendChild(el('span', 'skill-name', esc(skill.title)));
        var editBtn = el('button', 'skill-edit-btn', 'Edit');
        editBtn.addEventListener('click', function () { showBuilder(skill); });
        head.appendChild(editBtn);
        var delBtn = el('button', 'skill-del-btn', '×');
        delBtn.title = 'Delete ' + skill.title;
        head.appendChild(delBtn);
        card.appendChild(head);

        card.appendChild(el('p', 'skill-desc', esc(skill.description)));

        var confirmRow = el('div', 'skill-del-confirm');
        confirmRow.hidden = true;
        confirmRow.appendChild(el('span', 'skill-del-label', '⚠ Delete "' + esc(skill.title) + '"? Cannot be undone.'));
        var confirmYes = el('button', 'skill-del-yes', 'Delete');
        var confirmNo = el('button', 'secondary', 'Cancel');
        confirmRow.appendChild(confirmYes);
        confirmRow.appendChild(confirmNo);
        card.appendChild(confirmRow);

        delBtn.addEventListener('click', function () { confirmRow.hidden = false; delBtn.disabled = true; });
        confirmNo.addEventListener('click', function () { confirmRow.hidden = true; delBtn.disabled = false; });
        confirmYes.addEventListener('click', function () {
          fetch('/api/skills/' + encodeURIComponent(skill.id), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (d) { if (d.ok) renderSkills(d.skills); })
            .catch(function () {});
        });

        listEl.appendChild(card);
      });
    }

    function loadSkills() {
      getJSON('/api/skills').then(function (d) { renderSkills(d.skills || []); }).catch(function () {});
    }

    document.addEventListener('ayre:nav', function (e) {
      if (e.detail && e.detail.view === 'tools') loadSkills();
    });
    loadSkills();
  })();

})();
