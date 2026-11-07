;; Minimal Emacs for the org-mcp sidecar.  This is a server, not an editor:
;; no Doom, no blank-line normaliser, no git-auto-commit-mode.

(add-to-list 'load-path "/opt/emacs-lisp/mcp-server-lib.el")
(add-to-list 'load-path "/opt/emacs-lisp/org-mcp")

(require 'org)
(require 'org-id)
(require 'org-archive)

(setq org-directory "/org")
(setq org-agenda-files
      (list "/org/actionable.org"
            "/org/inbox.org"
            "/org/calendar.org"
            "/org/reference.org"
            "/org/topics.org"
            "/org/projects.org"))
(setq org-todo-keywords
      '((sequence "TODO(t)" "NEXT(n)" "TICKLE(l)" "WAIT(w)" "RESEARCH(r)"
                  "PROJ(p)" "IDEA(i)" "|" "DONE(d)" "KILL(k)")))
(setq org-archive-location "archive/%s_archive::")

;; Fixed filters for org-get-agenda.  The tool accepts a dispatch key as
;; `view'; custom-command views ignore the `date' argument, so use the
;; built-in day/week/month spans for pagination.
(setq org-agenda-custom-commands
      '(("d" "Today & Next"
         ((agenda "" ((org-agenda-span 'day)))
          (todo "NEXT")))
        ("n" "Next actions" todo "NEXT")
        ("t" "Open TODOs" todo "TODO")
        ("l" "Ticklers" todo "TICKLE")
        ("r" "Research" todo "RESEARCH")
        ("i" "Ideas" todo "IDEA")
        ("p" "Projects" todo "PROJ")
        ("a" "All open tasks" alltodo "")
        ("w" "Projects (projects.org)" alltodo ""
         ((org-agenda-files '("/org/projects.org"))))))

;; Everything in the clone is writable, including external/ (ID properties are
;; added there for id-linking) and the archives.  Git history is the recovery
;; net.  .attach/ is binary noise for grep; .git and the Syncthing leftovers
;; are not org content.  Repository agent notes stay on disk but are not in
;; this list: they are not Org files, and membership bought nothing because
;; agents read the rules from the skills.
(setq org-mcp-allowed-files
      (directory-files-recursively
       "/org" "\\.org\\(?:_archive\\)?\\'" nil
       (lambda (dir)
         (not (string-match-p
               "/\\.\\(git\\|attach\\|stversions\\|stfolder\\)\\(?:/\\|$\\)"
               dir)))))

(require 'mcp-server-lib-commands)
(require 'org-mcp)

(add-to-list 'load-path "/opt/emacs-lisp")
(require 'org-git-tool)

(defun my/org-mcp-agenda-filter (files)
  "Drop external and archive files from the agenda scan.
`org-get-agenda' uses `org-mcp-allowed-files', not `org-agenda-files';
keeping external in the allow-list preserves read/write and id-linking
without making every agenda request scan 99 MB.  Directories are resolved
first so a symlinked external/ is still recognised."
  (let ((archive (file-truename "/org/archive"))
        (external (file-truename "/org/external")))
    (seq-filter
     (lambda (file)
       (let ((file (file-truename file)))
         (and (not (string-match-p "_archive\\'" file))
              (not (string-prefix-p (concat archive "/") file))
              (not (string-prefix-p (concat external "/") file)))))
     files)))

(advice-add 'org-mcp--agenda-allowed-file-list
            :filter-return #'my/org-mcp-agenda-filter)

(defun my/org-mcp-start ()
  "Register org-mcp if needed and start the MCP server."
  (unless (mcp-server-lib-server-registered-p "org-mcp")
    (org-mcp-enable))
  (my/org-git-tool-enable)
  (unless (bound-and-true-p mcp-server-lib--running)
    (mcp-server-lib-start)))
