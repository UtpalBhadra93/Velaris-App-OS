-- F-07 (first slice) — the sovereign model marketplace.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/189. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it).
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * BOOLEAN becomes TINYINT(1); NUMERIC becomes DECIMAL.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS model_packages (
    id               CHAR(36)     PRIMARY KEY,
    tenant_id        CHAR(36)     NOT NULL,

    direction        VARCHAR(16)  NOT NULL,
    kind             VARCHAR(20)  NOT NULL,

    package_id       VARCHAR(255) NOT NULL,
    version          INT          NOT NULL DEFAULT 1,

    content_sha256   VARCHAR(64)  NOT NULL,

    manifest         JSON         NOT NULL,
    payload          JSON         NOT NULL,

    signature        TEXT         NULL,
    signing_key_id   VARCHAR(128) NULL,

    publisher_ref    VARCHAR(255) NULL,
    publisher_name   VARCHAR(255) NULL,

    source_ref       VARCHAR(255) NULL,
    trained_on       INT          NULL,
    declared_metrics JSON         NULL,
    eval_fingerprint VARCHAR(64)  NULL,

    canary_verdict   VARCHAR(20)  NOT NULL DEFAULT 'not_run',
    canary_detail    JSON         NULL,

    data_classes     JSON         NULL,
    special_category TINYINT(1)   NOT NULL DEFAULT 0,

    signed_off_by    VARCHAR(255) NULL,
    signed_off_at    DATETIME(6)  NULL,

    status           VARCHAR(20)  NOT NULL DEFAULT 'draft',
    status_reason    TEXT         NULL,

    created_by       VARCHAR(255) NULL,
    created_at       DATETIME(6)  NOT NULL,
    updated_at       DATETIME(6)  NOT NULL,

    KEY ix_model_pkg_tenant  (tenant_id, direction, status),
    KEY ix_model_pkg_content (content_sha256),

    CONSTRAINT ux_model_pkg_tenant_content UNIQUE (tenant_id, direction, content_sha256),
    CONSTRAINT fk_model_pkg_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS model_package_evals (
    id             CHAR(36)     PRIMARY KEY,
    package_ref    CHAR(36)     NOT NULL,
    tenant_id      CHAR(36)     NOT NULL,

    holdout_size   INT          NOT NULL DEFAULT 0,
    positives      INT          NOT NULL DEFAULT 0,
    negatives      INT          NOT NULL DEFAULT 0,

    declared       JSON         NULL,
    measured       JSON         NULL,
    tolerance      DECIMAL(4,3) NULL,

    verdict        VARCHAR(20)  NOT NULL,
    reason         TEXT         NULL,

    evaluated_by   VARCHAR(255) NULL,
    evaluated_at   DATETIME(6)  NOT NULL,

    KEY ix_model_eval_pkg (package_ref, evaluated_at),

    CONSTRAINT fk_model_eval_pkg FOREIGN KEY (package_ref)
        REFERENCES model_packages (id) ON DELETE CASCADE,
    CONSTRAINT fk_model_eval_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS model_package_installs (
    id             CHAR(36)     PRIMARY KEY,
    package_ref    CHAR(36)     NOT NULL,
    tenant_id      CHAR(36)     NOT NULL,

    model_origin   VARCHAR(20)  NOT NULL DEFAULT 'marketplace',

    target_kind    VARCHAR(20)  NOT NULL,
    target_ref     VARCHAR(255) NULL,

    eval_ref       CHAR(36)     NULL,

    status         VARCHAR(20)  NOT NULL DEFAULT 'staged',

    installed_by   VARCHAR(255) NULL,
    installed_at   DATETIME(6)  NOT NULL,
    activated_by   VARCHAR(255) NULL,
    activated_at   DATETIME(6)  NULL,
    uninstalled_at DATETIME(6)  NULL,
    shredded_at    DATETIME(6)  NULL,
    shred_reason   VARCHAR(64)  NULL,

    KEY ix_model_install_tenant (tenant_id, status),

    CONSTRAINT ux_model_install_pkg UNIQUE (tenant_id, package_ref),
    CONSTRAINT fk_model_install_pkg FOREIGN KEY (package_ref)
        REFERENCES model_packages (id) ON DELETE CASCADE,
    CONSTRAINT fk_model_install_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_model_install_eval FOREIGN KEY (eval_ref)
        REFERENCES model_package_evals (id) ON DELETE SET NULL
);


INSERT IGNORE INTO arbiter_namespaces
    (namespace, default_effect, max_mode, description, updated_at) VALUES
    ('model', 'deny', 'shadow',
     'Model packages: what may be published from here, and whose models may score here.',
     UTC_TIMESTAMP(6));
