-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : platform-wide latest-wins timestamp precision.
--
-- Follow-on to 0004 (which fixed the two HxSentinel append-only tables). MySQL/
-- MariaDB `DATETIME` defaults to SECOND precision, whereas PostgreSQL `TIMESTAMPTZ`
-- is microsecond. Any "newest row wins" / "pick the latest" query that orders by an
-- insert/event timestamp (`ORDER BY <ts> DESC ... LIMIT 1`, `MAX(<ts>)`, latest-per-
-- group) becomes non-deterministic on MySQL/MariaDB when two rows land in the same
-- second — the tiebreak then falls to a random UUID id (correct on Postgres, wrong
-- here). The most load-bearing case is `case_assignments.assigned_at` (current active
-- assignment / lock ownership); the rest are converted for platform-wide consistency
-- and future-proofing (DATETIME(6) is strictly more precise and never harms a
-- display-only column).
--
-- Scope: the primary insert/event timestamp of every table carrying one (both
-- append-only and mutable-state tables — the `created_at`/`timestamp`/`occurred_at`/
-- etc. column). `case_outcomes` and `case_risk_feedback` were already done in 0004.
--
-- Fresh installs already get DATETIME(6) from the regenerated 0001 baseline; this
-- migration upgrades EXISTING installs. `MODIFY ... DATETIME(6)` is idempotent — a
-- no-op when the column is already microsecond. Postgres is unaffected (already µs);
-- there is no Postgres counterpart to this file.
--
-- OPERATIONAL NOTE: `MODIFY DATETIME -> DATETIME(6)` rebuilds the table (not an
-- INSTANT/online-DDL change on all versions). Instant on empty/small tables, but a
-- few targets are high-volume append-only logs (case_event_log, telemetry_events,
-- trace_events, case_audit_log, security_events) — on a large existing install these
-- ALTERs lock/rebuild per table and may need a maintenance window or an online-DDL
-- tool (pt-online-schema-change / gh-ost). type+nullability FULLY specify each column
-- (no comment/onupdate/server_default on any target), so a 0005-upgraded install is
-- byte-identical to a fresh baseline install.
-- ============================================================================

ALTER TABLE access_groups                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE access_roles                           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE app_deployments                        MODIFY deployed_at DATETIME(6) NOT NULL;
ALTER TABLE app_packages                           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE artifact_analyses                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE artifact_branches                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE audit_anchors                          MODIFY anchored_at DATETIME(6) NOT NULL;
ALTER TABLE auth_devices                           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE auth_otp                               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE bpm_concepts                           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE branch_audit_events                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE branch_reviews                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE business_calendars                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_assignments                       MODIFY assigned_at DATETIME(6) NOT NULL;
ALTER TABLE case_audit_log                         MODIFY timestamp DATETIME(6) NOT NULL;
ALTER TABLE case_audit_log_chain                   MODIFY sealed_at DATETIME(6) NOT NULL;
ALTER TABLE case_event_log                         MODIFY timestamp DATETIME(6) NOT NULL;
ALTER TABLE case_instances                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_messages                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_outcome_labels                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_relationships                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_findings                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_models                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_rule_proposals               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_score_disputes               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_signal_packs                 MODIFY installed_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_signals                      MODIFY computed_at DATETIME(6) NOT NULL;
ALTER TABLE case_session_caption_segments          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_session_intelligence              MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_session_kyc_signals               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_session_participants              MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_sessions                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_sla_instances                     MODIFY started_at DATETIME(6) NOT NULL;
ALTER TABLE case_time_entries                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_type_variables                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_types                             MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE checkout_notifications_log             MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE checkout_orders                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE checkout_service_tokens                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE checkout_webhook_events                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE checkout_webhook_integrations          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE connector_registry                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE crm_sync_records                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE data_lineage_events                    MODIFY at DATETIME(6) NOT NULL;
ALTER TABLE data_models                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE db_manager_query_log                   MODIFY ran_at DATETIME(6) NOT NULL;
ALTER TABLE dead_letter_queue                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE deployment_runs                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE doc_extraction_jobs                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE doc_storage_routes                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE document_verifications                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE document_versions                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE documents                              MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE email_accounts                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE email_messages                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE email_templates                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE environment_registry                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE escalation_trees                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE esign_requests                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE field_population_audit                 MODIFY populated_at DATETIME(6) NOT NULL;
ALTER TABLE form_definitions                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE gdpr_requests                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE graph_edges                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE graph_nodes                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE helix_users                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxcanvas_boards                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxcanvas_items                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdbmigrate_analyses                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdbmigrate_migration_runs             MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdbmigrate_row_links                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdbmigrate_sources                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdocs_articles                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxdocs_spaces                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxevolve_baselines                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxevolve_insights                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxguard_tuples                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxnexus_conversations                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxnexus_document_chunks                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxnexus_messages                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxtest_results                         MODIFY ran_at DATETIME(6) NOT NULL;
ALTER TABLE hxtest_runs                            MODIFY started_at DATETIME(6) NOT NULL;
ALTER TABLE hxtest_suites                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxwork_boards                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxwork_card_relations                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxwork_sprints                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE hxwork_stories                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE identity_verifications                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE import_jobs                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE intake_events                          MODIFY received_at DATETIME(6) NOT NULL;
ALTER TABLE integration_calls                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE invoice_records                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_blacklist                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_containers                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_installs                   MODIFY installed_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_network_log                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_release_requests           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_sandbox_datasets           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_sources                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_whitelist                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_workspace_items            MODIFY installed_at DATETIME(6) NOT NULL;
ALTER TABLE marketplace_workspaces                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE mcp_action_proposals                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE mcp_idempotency_keys                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE mcp_token_grants                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE migration_pipeline_runs                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE migration_projects                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE migration_scans                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE migration_tasks                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE notification_logs                      MODIFY sent_at DATETIME(6) NOT NULL;
ALTER TABLE operator_access_groups                 MODIFY assigned_at DATETIME(6) NOT NULL;
ALTER TABLE outbound_connector_rules               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE outbox                                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE payment_disbursements                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE payment_requests                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE payment_webhook_events                 MODIFY received_at DATETIME(6) NOT NULL;
ALTER TABLE pipeline_stage_events                  MODIFY started_at DATETIME(6);
ALTER TABLE platform_update_plans                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE platform_update_runs                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE portal_ask_feedback                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE portal_csat                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE portal_customers                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE portal_submission_refs                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE portals                                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE process_case_bindings                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE process_definitions                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE process_instances                      MODIFY started_at DATETIME(6) NOT NULL;
ALTER TABLE process_task_log                       MODIFY started_at DATETIME(6) NOT NULL;
ALTER TABLE push_device_tokens                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE rate_cards                             MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE refresh_tokens                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE region_access_log                      MODIFY recorded_at DATETIME(6) NOT NULL;
ALTER TABLE region_health_log                      MODIFY recorded_at DATETIME(6) NOT NULL;
ALTER TABLE region_registry                        MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE replay_results                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE replay_runs                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE report_subscriptions                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE retention_policies                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE rule_definitions                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE saved_reports                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE scheduled_releases                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE security_events                        MODIFY timestamp DATETIME(6) NOT NULL;
ALTER TABLE security_rules                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE shield_events                          MODIFY recorded_at DATETIME(6) NOT NULL;
ALTER TABLE slack_notifications                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sms_messages                           MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sovereignty_rules                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sso_providers                          MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_analytics_events            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_domains                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_inventory_log               MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_media                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_pages                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_products                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_promotions                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_stores                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_subscribers                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE storefront_themes                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sync_destinations                      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sync_field_mappings                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sync_redaction_rules                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE sync_runs                              MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE telemetry_events                       MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE tenant_deks                            MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE tenant_memberships                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE tenant_region_assignments              MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE tenants                                MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE trace_events                           MODIFY occurred_at DATETIME(6) NOT NULL;
ALTER TABLE user_directory                         MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE variable_namespaces                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE webauthn_challenges                    MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE webauthn_credentials                   MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE webhook_deliveries                     MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE webhook_receiver_events                MODIFY received_at DATETIME(6) NOT NULL;
ALTER TABLE webhook_receiver_rules                 MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE webhook_subscriptions                  MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE work_queues                            MODIFY created_at DATETIME(6) NOT NULL;
