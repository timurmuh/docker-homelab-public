;;; check-org-git-tool.el --- batch checks for org-git-tool -*- lexical-binding: t; -*-

;; Run from the repo that contains this file:
;;   emacs --batch -L ~/.emacs.d/.local/straight/repos/mcp-server-lib.el \
;;         -l check-org-git-tool.el
;;
;; Preconditions: the org directory named by ORG_GIT_TEST_ROOT (default
;; ~/org) is a git repository with at least three commits and a file named
;; inbox.org in its root.  The fixture tests build their own repositories in
;; a temporary directory and do not touch it.

(require 'cl-lib)
(require 'subr-x)
;; `org-directory' must be a special variable for the fixture tests below to
;; rebind it; load org so it is declared.
(require 'org)

(setq org-directory
      (file-name-as-directory
       (or (getenv "ORG_GIT_TEST_ROOT") (expand-file-name "~/org"))))

(add-to-list 'load-path
             (file-name-directory (or load-file-name buffer-file-name)))
(require 'org-git-tool)

(defvar my/check-failures 0)

(defun my/check (label form &optional predicate)
  "Evaluate FORM; PREDICATE (default non-empty string) decides pass."
  (condition-case err
      (let ((value (eval form t)))
        (if (funcall (or predicate (lambda (v) (and (stringp v)
                                                    (not (string-empty-p v)))))
                     value)
            (princ (format "PASS %s\n" label))
          (setq my/check-failures (1+ my/check-failures))
          (princ (format "FAIL %s -> unexpected value %S\n"
                         label (string-truncate-left (format "%s" value) 200)))))
    (error
     (setq my/check-failures (1+ my/check-failures))
     (princ (format "FAIL %s -> error %S\n" label err)))))

(defun my/check-error (label form substring)
  "Evaluate FORM, require a signal, and require SUBSTRING in its message."
  (condition-case err
      (let ((value (eval form t)))
        (setq my/check-failures (1+ my/check-failures))
        (princ (format "FAIL %s -> expected an error, got %S\n"
                       label (string-truncate-left (format "%s" value) 120))))
    (error
     (let ((message (error-message-string err)))
       (if (string-match-p (regexp-quote substring) message)
           (princ (format "PASS %s -> %s\n" label (string-truncate-left message 90)))
         (setq my/check-failures (1+ my/check-failures))
         (princ (format "FAIL %s -> error did not mention %S: %s\n"
                        label substring (string-truncate-left message 120))))))))

(defun my/count-lines (text)
  (length (seq-filter (lambda (line) (not (string-empty-p line)))
                      (split-string text "\n"))))

(defun my/commit-lines (text)
  "Return the commit lines of TEXT, ignoring a trailing truncation line."
  (seq-filter (lambda (line) (string-match-p "\\`[0-9a-f]\\{7,\\} " line))
              (split-string text "\n")))

(defun my/commit-count (text)
  (length (my/commit-lines text)))

(defun my/truncated-p (text)
  (and (string-match-p "(truncated at [0-9]+ commits" text) t))

(defun my/git (dir &rest args)
  (with-temp-buffer
    (let ((default-directory dir))
      (unless (zerop (apply #'process-file "git" nil t nil args))
        (error "git %s failed: %s" args (buffer-string))))))

(defun my/write-file (dir name text)
  (with-temp-file (expand-file-name name dir) (insert text)))

(defun my/commit (dir message)
  (my/git dir "add" "-A")
  (my/git dir "commit" "-qm" message))

(defun my/commit-at (dir message date)
  "Commit everything in DIR as MESSAGE, with DATE for author and committer."
  (my/git dir "add" "-A")
  (let ((process-environment
         (cons (concat "GIT_AUTHOR_DATE=" date)
               (cons (concat "GIT_COMMITTER_DATE=" date) process-environment))))
    (my/git dir "commit" "-qm" message)))

(defun my/make-repo ()
  (let ((dir (make-temp-file "org-git-tool-test-" t)))
    (my/git dir "init" "-q")
    (my/git dir "config" "user.name" "test")
    (my/git dir "config" "user.email" "test@example.com")
    dir))

;; --- the real org repository -------------------------------------------

(my/check "repo-wide log returns 3 commits"
          '(my/org-git-log nil 3 nil)
          (lambda (v) (= 3 (my/commit-count v))))

(my/check "repo-wide log lines look like oneline output"
          '(my/org-git-log nil 3 nil)
          (lambda (v)
            (and (cl-every (lambda (line)
                             (string-match-p "\\`[0-9a-f]\\{7,\\} " line))
                           (my/commit-lines v))
                 ;; Reaching the limit is signalled, and nothing else is added.
                 (my/truncated-p v)
                 (= 4 (my/count-lines v)))))

(my/check "absolute file path inside org"
          `(my/org-git-log ,(expand-file-name "inbox.org" org-directory) 3 nil))

(my/check "repo-relative file path"
          '(my/org-git-log "inbox.org" 3 nil))

(my/check "container-style /org alias"
          '(my/org-git-log "/org/inbox.org" 3 nil))

(my/check "patch mode includes a diff"
          '(my/org-git-log "inbox.org" 1 t)
          (lambda (v) (string-match-p "^diff --git" v)))

(my/check "limit is honoured"
          '(my/org-git-log nil 2 nil)
          (lambda (v) (= 2 (my/commit-count v))))

(my/check-error "existing path outside org is rejected"
                '(my/org-git-log "/etc/hosts" 3 nil)
                "outside the org directory")

(my/check-error "missing path outside org is rejected without leaking existence"
                '(my/org-git-log "/etc/definitely-not-here-9c1f" 3 nil)
                "outside the org directory")

(my/check-error "missing path inside org is rejected"
                '(my/org-git-log "no-such-file.org" 3 nil)
                "No such path")

(my/check-error "traversal is rejected"
                '(my/org-git-log "../org-nonexistent-9c1f" 3 nil)
                "outside the org directory")

(my/check-error "git metadata is rejected"
                '(my/org-git-log ".git/config" 3 nil)
                "git metadata")

;; --- a directory that is not a repository root -------------------------

(let ((repo (my/make-repo)))
  (my/write-file repo "a.org" "* a\n")
  (my/commit repo "first")
  (make-directory (expand-file-name "sub" repo) t)
  (my/write-file (expand-file-name "sub" repo) "b.org" "* b\n")
  (my/commit repo "second")
  (let ((org-directory (file-name-as-directory (expand-file-name "sub" repo))))
    (my/check-error "a subdirectory of a repository is refused"
                    '(my/org-git-log nil 5 nil)
                    "not the repository root")))

;; --- a filename that looks like a flag ---------------------------------

(let ((repo (my/make-repo)))
  (my/write-file repo "other.org" "* other\n")
  (my/commit repo "first commit")
  (my/write-file repo "--all" "* dash\n")
  (my/commit repo "second commit")
  (let ((org-directory (file-name-as-directory repo)))
    (my/check "leading-dash filename is a path, not a flag"
              '(my/org-git-log "--all" 10 nil)
              (lambda (v)
                (and (= 1 (my/commit-count v))
                     (string-match-p "second commit" v)
                     (not (string-match-p "first commit" v))
                     (not (my/truncated-p v)))))))

;; --- a repository that configures a diff driver ------------------------

(let ((repo (my/make-repo))
      (marker nil))
  (setq marker (expand-file-name "TEXTCONV-RAN" repo))
  (my/write-file repo ".gitattributes" "*.org diff=evil\n")
  (my/git repo "config" "diff.evil.textconv" (format "touch %s; cat" marker))
  (my/write-file repo "a.org" "* one\n")
  (my/commit repo "first")
  (my/write-file repo "a.org" "* two\n")
  (my/commit repo "second")
  (let ((org-directory (file-name-as-directory repo)))
    (my/check "patch mode still produces a diff"
              '(my/org-git-log "a.org" 1 t)
              (lambda (v) (string-match-p "^diff --git" v)))
    (my/check "patch mode does not run a configured textconv"
              `(progn (my/org-git-log "a.org" 1 t)
                      (if (file-exists-p ,marker) "RAN" "not run"))
              (lambda (v) (string= v "not run")))))

;; --- since, stat and the truncation notice ------------------------------

(let ((repo (my/make-repo)))
  (my/write-file repo "old.org" "* old\n")
  (my/commit-at repo "old commit" "2020-01-01T12:00:00+0000")
  (my/write-file repo "middle.org" "* middle\n")
  (my/commit-at repo "middle commit" "2021-01-01T12:00:00+0000")
  (my/write-file repo "new.org" "* new\n")
  (my/commit-at repo "new commit" "2022-01-01T12:00:00+0000")
  (let ((org-directory (file-name-as-directory repo)))
    (my/check "since returns only the commits after the date"
              '(my/org-git-log nil 10 nil nil "2021-06-01")
              (lambda (v)
                (and (= 1 (my/commit-count v))
                     (string-match-p "new commit" v)
                     (not (string-match-p "old commit" v)))))
    (my/check "since that matches everything keeps all commits"
              '(my/org-git-log nil 10 nil nil "2019-01-01")
              (lambda (v) (= 3 (my/commit-count v))))
    ;; Documented, not assumed: git accepts a nonsense approxidate without
    ;; an error, and the filter resolves to nothing.
    (my/check "nonsense since is silent and returns no commits"
              '(my/org-git-log nil 10 nil nil "not-a-date")
              (lambda (v) (string= v "No commits matched.")))
    (my/check "stat returns a file summary, not a diff"
              '(my/org-git-log "new.org" 1 nil t)
              (lambda (v)
                (and (string-match-p "file changed" v)
                     (not (string-match-p "^diff --git" v)))))
    (my/check-error "stat and patch together are rejected"
                    '(my/org-git-log nil 1 t t)
                    "mutually exclusive")
    (my/check "a result at the limit carries the truncation line"
              '(my/org-git-log nil 3 nil)
              (lambda (v)
                (and (my/truncated-p v)
                     (string-match-p "(truncated at 3 commits" v)
                     (= 3 (my/commit-count v)))))
    (my/check "a result above the limit carries no truncation line"
              '(my/org-git-log nil 4 nil)
              (lambda (v)
                (and (not (my/truncated-p v))
                     (= 3 (my/commit-count v)))))))

(princ (format "\n%s: %d failure(s)\n"
               (if (zerop my/check-failures) "OK" "FAILED")
               my/check-failures))
(kill-emacs (if (zerop my/check-failures) 0 1))
