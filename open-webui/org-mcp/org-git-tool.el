;;; org-git-tool.el --- Read-only git log tool for org-mcp -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;; Author: Timur Mukhametulin
;; Keywords: org, tools
;; Package-Requires: ((emacs "28.2") (mcp-server-lib "0.4.0"))

;;; Commentary:

;; Adds one read-only tool, `org-git-log', to an already-registered
;; `org-mcp' MCP server.  The wiki uses git history as its change log, and
;; org-mcp exposes no history, so an agent asked "how did this note get
;; this way" otherwise has no answer.
;;
;; Safety properties, which are the whole reason this is a separate package
;; rather than a generic git runner:
;;
;;   - `process-file' with an argument vector; no shell, ever.
;;   - The subcommand is fixed to `log'.  There is no way to reach a git
;;     verb that writes.
;;   - Paths are resolved, checked with `file-in-directory-p' against
;;     `org-directory', and passed after `--'.
;;   - `default-directory' is `org-directory', so the same code works on the
;;     Mac (`~/org') and in the sidecar (`/org').
;;
;; Load this file after org-mcp and call `my/org-git-tool-enable' from the
;; same wrapper that calls `org-mcp-enable'.

;;; Code:

(require 'subr-x)
(require 'mcp-server-lib)

(defconst my/org-git-tool--max-limit 200
  "Upper bound on the number of commits a single call may return.")

(defvar my/org-git-tool--registered nil
  "Non-nil once this package has registered its tool.
Registration increments reference counts, so it must happen once per
Emacs process, not once per client connection.")

(defun my/org-git-tool--root ()
  "Return the truename of `org-directory' as a directory."
  (unless (and (boundp 'org-directory) org-directory)
    ;; Falling back to `default-directory' would run git in whatever the
    ;; daemon's cwd happens to be and enforce containment against that.
    (user-error "org-directory is not set; refusing to guess a repository"))
  (file-name-as-directory
   (file-truename (expand-file-name org-directory))))

(defun my/org-git-tool--assert-repo (root)
  "Signal unless ROOT is itself the top level of a git repository.
Without this, `git log' walks up to an enclosing repository and the tool
reports history that is not the org repository's."
  (with-temp-buffer
    (let ((default-directory root))
      (unless (zerop (process-file "git" nil t nil "rev-parse" "--show-toplevel"))
        (user-error "Not a git repository: %s" root)))
    (let ((toplevel (file-name-as-directory
                     (string-trim (buffer-string)))))
      (unless (string= (file-truename toplevel) (file-truename root))
        (user-error "org-directory is not the repository root: %s" root)))))

(defun my/org-git-tool--resolve (file)
  "Resolve FILE to a truename inside the org repository, or signal.
FILE may be an absolute path, a path relative to `org-directory', or a
`/org/...' path, which is read as repo-relative so the same call works on
the Mac and in the sidecar.  Returns nil when FILE is nil or blank."
  (when (and file (not (string-blank-p file)))
    (let* ((root (my/org-git-tool--root))
           (raw (string-trim file))
           (candidate
            (cond
             ((string-prefix-p "/org/" raw)
              (expand-file-name (substring raw (length "/org/")) root))
             ((file-name-absolute-p raw) raw)
             (t (expand-file-name raw root))))
           (parent
            (condition-case nil
                (file-truename
                 (file-name-directory (directory-file-name candidate)))
              (error nil))))
      ;; Containment is decided on the parent first, so the error message
      ;; does not tell the caller whether a path outside org exists.
      (unless (and parent (file-in-directory-p parent root))
        (user-error "Path is outside the org directory: %s" raw))
      (unless (file-exists-p candidate)
        (user-error "No such path in the org repository: %s" raw))
      (let ((true (file-truename candidate)))
        (unless (file-in-directory-p true root)
          (user-error "Path is outside the org directory: %s" raw))
        (when (string-match-p "/\\.git\\(?:/\\|\\'\\)" true)
          (user-error "Refusing to read git metadata: %s" raw))
        true))))

(defun my/org-git-tool--normalize-limit (limit)
  "Coerce LIMIT to a positive integer, defaulting to 20."
  (let ((n (cond ((null limit) 20)
                 ((numberp limit) (truncate limit))
                 ((and (stringp limit) (string-match-p "\\`[0-9]+\\'" limit))
                  (string-to-number limit))
                 (t 20))))
    (max 1 (min my/org-git-tool--max-limit n))))

(defun my/org-git-tool--normalize-patch (patch)
  "Return non-nil when PATCH asks for a diff."
  (and patch (not (eq patch :json-false)) (not (equal patch "false"))))

(defun my/org-git-tool--normalize-since (since)
  "Return SINCE trimmed, or nil when it is nil or blank."
  (when (and since (stringp since) (not (string-blank-p since)))
    (string-trim since)))

(defun my/org-git-tool--commit-count (text)
  "Count the commits in TEXT, a `git log' output.
One-line output has one line per commit; patch and stat output repeats a
`commit <sha>' header for each."
  (let ((lines (split-string text "\n" t)))
    (if (seq-some (lambda (line) (string-prefix-p "commit " line)) lines)
        (seq-count (lambda (line) (string-prefix-p "commit " line)) lines)
      (length lines))))

(defun my/org-git-log (&optional file limit patch stat since)
  "Show git history for the org repository.

Returns plain text: `git log --oneline' output by default, `--stat' when
STAT is true, or a full patch when PATCH is true.  Scope it to one file
with FILE; omit FILE for repo-wide history.  Filter by date with SINCE,
which takes git's approxidate format, e.g. \"2026-10-01\" or \"2 weeks
ago\".  The repository is always `org-directory'.

When the result reaches LIMIT, one line is appended so a partial history
is not reported as complete.

MCP Parameters:
  file - Optional path inside the org repository: an absolute path, a
         path relative to the org directory, or a `/org/...' path.
         Omit it, or pass an empty string, for repo-wide history.
         Paths outside the org directory are rejected.
  limit - Maximum number of commits to return (integer, optional,
          default 20, capped at 200).
  patch - When true, return the full patch instead of one line per
          commit (boolean, optional, default false).
  stat - When true, return a per-file stat summary instead of a diff
         (boolean, optional, default false).  Cannot be combined with
         patch.
  since - Only commits after this date, in git's approxidate format
          (string, optional).  Git does not report a malformed value;
          it silently ignores or misreads it."
  (mcp-server-lib-with-error-handling
   (let* ((root (my/org-git-tool--root))
          (path (my/org-git-tool--resolve file))
          (count (my/org-git-tool--normalize-limit limit))
          (want-patch (my/org-git-tool--normalize-patch patch))
          (want-stat (my/org-git-tool--normalize-patch stat))
          (since (my/org-git-tool--normalize-since since)))
     (when (and want-patch want-stat)
       ;; Silently preferring one would hand back a summary when the caller
       ;; asked for a diff, and the caller would not notice.
       (user-error "stat and patch are mutually exclusive"))
     (let ((args (append (list "log" "--no-color"
                               ;; A repo can configure a textconv or external
                               ;; diff driver, which spawns a program.  Refuse
                               ;; both: this tool runs nothing but git.
                               "--no-textconv" "--no-ext-diff"
                               (format "-n%d" count))
                         (cond (want-stat (list "--stat"))
                               (want-patch (list "-p"))
                               (t (list "--oneline")))
                         ;; `--since=' is one argv element, so a value that
                         ;; starts with `-' cannot be read as a flag.
                         (when since (list (concat "--since=" since)))
                         (when path (list "--" path)))))
       (my/org-git-tool--assert-repo root)
       (with-temp-buffer
         (let ((default-directory root))
           (let ((status (apply #'process-file "git" nil t nil args)))
             (cond
              ;; A string status means the child died on a signal or could not
              ;; be started; partial output is not an answer.
              ((not (integerp status))
               (user-error "git log did not complete: %s" status))
              ((not (zerop status))
               (user-error "git log failed (%d): %s"
                           status (string-trim (buffer-string))))
              ((string-empty-p (buffer-string)) "No commits matched.")
              (t
               (let ((text (buffer-string)))
                 (if (= (my/org-git-tool--commit-count text) count)
                     (concat text
                             (format "\n(truncated at %d commits; raise limit or narrow since)\n"
                                     count))
                   text)))))))))))

(defun my/org-git-tool-enable ()
  "Register `org-git-log' on the `org-mcp' server.
Idempotent: a second call in the same Emacs does nothing, because
`mcp-server-lib-register-server' increments reference counts and repeated
registration would make teardown unbalanced."
  (unless my/org-git-tool--registered
    (mcp-server-lib-register-server
     :id "org-mcp"
     :tools
     (list
      (list #'my/org-git-log
            :id "org-git-log"
            :description
            "Show git history for the org repository, optionally scoped to
one file.  Use this to answer how a note reached its current state, when
it changed, or what changed in it.  Read-only: it can only run git log.
Omit `file', or pass an empty string, to get repository-wide history;
pass a path to scope the log to that file.  Use `since' for a date range,
`stat' for a per-file summary, and `patch' for the diff; `stat' and
`patch' are mutually exclusive.  A result that reaches `limit' ends with a
truncation line."
            :title "Org git log"
            :read-only t)))
    (setq my/org-git-tool--registered t)))

(provide 'org-git-tool)

;;; org-git-tool.el ends here
