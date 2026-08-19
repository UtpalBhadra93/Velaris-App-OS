-- Omnichannel inbound — SMS, WhatsApp and voice metadata.
--
-- **One channel spine, not three channel features.** `email_messages` already
-- proves the bidirectional, nullable-case, threaded shape works, so the new
-- tables are modelled on it rather than invented.
--
-- `sms_messages` is deliberately NOT widened. That table is step-bound —
-- `case_id` and `step_id` are both NOT NULL — because it records "the workflow
-- sent this". An inbound message has no step and may have no case; adding a
-- `direction` column would produce a table where half the columns are
-- meaningless for half the rows. It keeps recording step-bound outbound sends
-- and is left untouched.
--
-- Email is NOT migrated onto these tables. Its threading model (`references`
-- chains) has no analogue in SMS, and a merge buys nothing.

-- The addressable thing a customer contacts — the "email account" equivalent.
CREATE TABLE IF NOT EXISTS channel_endpoints (
    id                     UUID         PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,
    channel                VARCHAR(20)  NOT NULL,   -- sms | whatsapp | voice
    provider               VARCHAR(30)  NOT NULL,   -- twilio | meta | generic
    address                VARCHAR(64)  NOT NULL,   -- E.164 number or WABA id
    connector_id           UUID,
    signing_secret_enc     JSONB,
    default_case_type_id   UUID,
    auto_create_case       BOOLEAN      NOT NULL DEFAULT TRUE,
    conversation_ttl_hours INTEGER      NOT NULL DEFAULT 72,
    routing                JSONB        NOT NULL DEFAULT '{}'::jsonb,
    is_active              BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT channel_endpoints_addr_uq UNIQUE (provider, channel, address)
);

CREATE INDEX IF NOT EXISTS ix_chep_tenant ON channel_endpoints(tenant_id, channel);

COMMENT ON COLUMN channel_endpoints.signing_secret_enc IS
    'Encrypted webhook signing secret (Twilio auth token / Meta app secret). '
    'The inbound route authenticates on this and nothing else.';

-- One thread with one peer on one endpoint.
CREATE TABLE IF NOT EXISTS channel_conversations (
    id                UUID         PRIMARY KEY,
    tenant_id         VARCHAR(255) NOT NULL,
    endpoint_id       UUID         NOT NULL REFERENCES channel_endpoints(id) ON DELETE CASCADE,
    peer_address      VARCHAR(128) NOT NULL,
    case_id           UUID         REFERENCES case_instances(id) ON DELETE SET NULL,
    customer_id       UUID,
    status            VARCHAR(20)  NOT NULL DEFAULT 'open',   -- open | assigned | closed
    assigned_to       VARCHAR(255),
    window_expires_at TIMESTAMPTZ,
    last_message_at   TIMESTAMPTZ,
    unread_count      INTEGER      NOT NULL DEFAULT 0,
    metadata_json     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_chconv_lookup ON channel_conversations(endpoint_id, peer_address, status);
CREATE INDEX IF NOT EXISTS ix_chconv_case   ON channel_conversations(case_id);
CREATE INDEX IF NOT EXISTS ix_chconv_tenant ON channel_conversations(tenant_id, last_message_at);

COMMENT ON COLUMN channel_conversations.window_expires_at IS
    'WhatsApp 24-hour session window. Outside it only a pre-approved template '
    'may be sent; unmodelled, replies fail silently at the provider.';

CREATE TABLE IF NOT EXISTS channel_messages (
    id                  UUID         PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL,
    conversation_id     UUID         NOT NULL REFERENCES channel_conversations(id) ON DELETE CASCADE,
    case_id             UUID         REFERENCES case_instances(id) ON DELETE SET NULL,
    channel             VARCHAR(20)  NOT NULL,
    direction           VARCHAR(10)  NOT NULL,   -- inbound | outbound
    author              VARCHAR(255),            -- 'user:{id}' | 'customer:{id}' | NULL
    peer_address        VARCHAR(128) NOT NULL,
    body                TEXT         NOT NULL,
    media               JSONB        NOT NULL DEFAULT '[]'::jsonb,
    provider_message_id VARCHAR(255),
    template_name       VARCHAR(255),
    status              VARCHAR(30)  NOT NULL,
    error               TEXT,
    raw_payload         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    is_read             BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP(6) NOT NULL DEFAULT now(),
    sent_at             TIMESTAMPTZ,
    received_at         TIMESTAMPTZ
);

-- **This unique index IS the idempotency guarantee.** Twilio and Meta both
-- retry deliveries; without it a retry creates a second case. Same role
-- `message_id` plays for email. Partial, because outbound rows have no provider
-- id until the send returns and NULLs must not collide.
CREATE UNIQUE INDEX IF NOT EXISTS ix_chmsg_provider_id
    ON channel_messages(provider_message_id) WHERE provider_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_chmsg_conv   ON channel_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_chmsg_case   ON channel_messages(case_id);
CREATE INDEX IF NOT EXISTS ix_chmsg_unread ON channel_messages(tenant_id, is_read);

CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id                   UUID         PRIMARY KEY,
    tenant_id            VARCHAR(255) NOT NULL,
    endpoint_id          UUID         NOT NULL REFERENCES channel_endpoints(id) ON DELETE CASCADE,
    name                 VARCHAR(255) NOT NULL,
    language             VARCHAR(10)  NOT NULL,
    provider_template_id VARCHAR(255),
    body_params          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    approval_status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at           TIMESTAMP(6) NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_templates_uq UNIQUE (endpoint_id, name, language)
);
