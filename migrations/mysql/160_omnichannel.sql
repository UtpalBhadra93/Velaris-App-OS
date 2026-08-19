-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : omnichannel inbound
-- (PostgreSQL migration 160).
--
-- One channel spine, not three channel features. `email_messages` already
-- proves the bidirectional, nullable-case, threaded shape works, so these
-- tables are modelled on it.
--
-- `sms_messages` is deliberately NOT widened: it is step-bound (`case_id` and
-- `step_id` both NOT NULL) because it records "the workflow sent this". An
-- inbound message has no step and may have no case.
--
-- Dialect notes: CHAR(36) is what `GUID` emits outside PostgreSQL, JSON what
-- `PortableJSON` emits, DATETIME(6) matches `MicroDateTime`. MySQL has no
-- partial index, so `provider_message_id` gets a plain UNIQUE KEY — which is
-- equivalent here, because MySQL and MariaDB both allow repeated NULLs in a
-- unique index, exactly as the PostgreSQL partial index does.
-- ============================================================================

CREATE TABLE IF NOT EXISTS channel_endpoints (
    id                     CHAR(36)     NOT NULL,
    tenant_id              VARCHAR(255) NOT NULL,
    channel                VARCHAR(20)  NOT NULL,
    provider               VARCHAR(30)  NOT NULL,
    address                VARCHAR(64)  NOT NULL,
    connector_id           CHAR(36)     NULL,
    signing_secret_enc     JSON         NULL,
    default_case_type_id   CHAR(36)     NULL,
    auto_create_case       TINYINT(1)   NOT NULL DEFAULT 1,
    conversation_ttl_hours INT          NOT NULL DEFAULT 72,
    routing                JSON         NOT NULL,
    is_active              TINYINT(1)   NOT NULL DEFAULT 1,
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY channel_endpoints_addr_uq (provider, channel, address),
    KEY ix_chep_tenant (tenant_id, channel)
);

CREATE TABLE IF NOT EXISTS channel_conversations (
    id                CHAR(36)     NOT NULL,
    tenant_id         VARCHAR(255) NOT NULL,
    endpoint_id       CHAR(36)     NOT NULL,
    peer_address      VARCHAR(128) NOT NULL,
    case_id           CHAR(36)     NULL,
    customer_id       CHAR(36)     NULL,
    status            VARCHAR(20)  NOT NULL DEFAULT 'open',
    assigned_to       VARCHAR(255) NULL,
    window_expires_at DATETIME     NULL,
    last_message_at   DATETIME     NULL,
    unread_count      INT          NOT NULL DEFAULT 0,
    metadata_json     JSON         NOT NULL,
    created_at        DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_chconv_lookup (endpoint_id, peer_address, status),
    KEY ix_chconv_case (case_id),
    KEY ix_chconv_tenant (tenant_id, last_message_at),
    CONSTRAINT channel_conversations_endpoint_fk
        FOREIGN KEY (endpoint_id) REFERENCES channel_endpoints (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS channel_messages (
    id                  CHAR(36)     NOT NULL,
    tenant_id           VARCHAR(255) NOT NULL,
    conversation_id     CHAR(36)     NOT NULL,
    case_id             CHAR(36)     NULL,
    channel             VARCHAR(20)  NOT NULL,
    direction           VARCHAR(10)  NOT NULL,
    author              VARCHAR(255) NULL,
    peer_address        VARCHAR(128) NOT NULL,
    body                TEXT         NOT NULL,
    media               JSON         NOT NULL,
    provider_message_id VARCHAR(255) NULL,
    template_name       VARCHAR(255) NULL,
    status              VARCHAR(30)  NOT NULL,
    error               TEXT         NULL,
    raw_payload         JSON         NOT NULL,
    is_read             TINYINT(1)   NOT NULL DEFAULT 0,
    created_at          DATETIME(6)  NOT NULL,
    sent_at             DATETIME     NULL,
    received_at         DATETIME     NULL,
    PRIMARY KEY (id),
    -- The idempotency guarantee: Twilio and Meta both retry, and a retry must
    -- not create a second case. Repeated NULLs are allowed in a MySQL unique
    -- index, so outbound rows awaiting a provider id do not collide.
    UNIQUE KEY ix_chmsg_provider_id (provider_message_id),
    KEY ix_chmsg_conv (conversation_id, created_at),
    KEY ix_chmsg_case (case_id),
    KEY ix_chmsg_unread (tenant_id, is_read),
    CONSTRAINT channel_messages_conv_fk
        FOREIGN KEY (conversation_id) REFERENCES channel_conversations (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id                   CHAR(36)     NOT NULL,
    tenant_id            VARCHAR(255) NOT NULL,
    endpoint_id          CHAR(36)     NOT NULL,
    name                 VARCHAR(255) NOT NULL,
    language             VARCHAR(10)  NOT NULL,
    provider_template_id VARCHAR(255) NULL,
    body_params          JSON         NOT NULL,
    approval_status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at           DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY whatsapp_templates_uq (endpoint_id, name, language),
    CONSTRAINT whatsapp_templates_endpoint_fk
        FOREIGN KEY (endpoint_id) REFERENCES channel_endpoints (id) ON DELETE CASCADE
);
