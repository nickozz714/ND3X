ALLOWED_TOOLS = {
    # existing
    "code_search",
    "text_search",
    "text_ingest",
    "text_update",
    "text_delete",
    "sync_now",

    # -----------------------------
    # todo (lists)
    # -----------------------------
    "todo_list_create",
    "todo_list_list",
    "todo_list_update",
    "todo_list_delete",

    # -----------------------------
    # todo (items)
    # -----------------------------
    "todo_item_create",
    "todo_item_list",
    "todo_today",
    "todo_item_update",
    "todo_item_delete",

    # -----------------------------
    # todo (supplements)
    # -----------------------------
    "todo_supplement_add",
    "todo_supplement_list",
    "todo_supplement_update",
    "todo_supplement_delete",

    # -----------------------------
    # todo (bulk)
    # -----------------------------
    "todo_bulk_move",
    "todo_bulk_status",
    "todo_bulk_priority",
    "todo_bulk_schedule",
    "todo_bulk_due_at",
    "todo_bulk_reorder",
    "todo_bulk_delete",
    "todo_clear_completed",

    # -----------------------------
    # project management (pm)
    # -----------------------------
    "pm_health",
    # projects
    "pm_project_create",
    "pm_project_get",
    "pm_project_update",
    "pm_project_delete",
    "pm_project_list",
    "pm_project_get_full",
    # planning: epics
    "pm_epic_create",
    "pm_epic_list",
    "pm_epic_update",
    "pm_epic_delete",
    # planning: features
    "pm_feature_create",
    "pm_feature_list",
    "pm_feature_update",
    "pm_feature_delete",
    # planning: workitems
    "pm_workitem_create",
    "pm_workitem_list",
    "pm_workitem_update",
    "pm_workitem_delete",
    # planning: tasks
    "pm_task_create",
    "pm_task_list",
    "pm_task_update",
    "pm_task_delete",
    # time tracking
    "pm_time_start",
    "pm_time_stop",
    # reports
    "pm_report_hours_by_task",
    "pm_report_hours_by_day_code",
    "pm_report_hours_by_day_code_all_projects",
    "pm_report_active_tasks",
    "pm_report_hours_by_code",

    # -----------------------------
    # data modelling (health)
    # -----------------------------
    "datamodel_health",
    # -----------------------------
    # data modelling (projects)
    # -----------------------------
    "project_create",
    "project_get",
    "project_update",
    "project_delete",
    "project_list",
    "project_get_full",
    # -----------------------------
    # data modelling (datasource credentials)
    # -----------------------------
    "datasource_credential_create",
    "datasource_credential_get",
    "datasource_credential_update",
    "datasource_credential_delete",
    # -----------------------------
    # data modelling (datasources)
    # -----------------------------
    "datasource_create",
    "datasource_get",
    "datasource_update",
    "datasource_delete",
    "datasource_list",
    "datasource_attach_credential",
    "datasource_detach_credential",
    # -----------------------------
    # data modelling (metamodels)
    # -----------------------------
    "metamodel_create",
    "metamodel_get",
    "metamodel_update",
    "metamodel_delete",
    "metamodel_list",
    "metamodel_clone",
    "metamodel_bump_version",
    "metamodel_tree",
    # -----------------------------
    # data modelling (structure)
    # -----------------------------
    "domain_create",
    "domain_update",
    "domain_delete",
    "domain_list",
    "section_create",
    "section_update",
    "section_delete",
    "section_list",
    "table_create",
    "table_update",
    "table_delete",
    "table_list",
    "column_create",
    "column_update",
    "column_delete",
    "column_list",
    "column_set_primary_key",
    "relation_create",
    "relation_update",
    "relation_delete",
    "relation_list_for_column",
    # -----------------------------
    # data modelling (mappings)
    # -----------------------------
    "mapping_create",
    "mapping_get",
    "mapping_update",
    "mapping_delete",
    "mapping_list",
    "mapping_find_by_source_target",
    "mapping_validate_refs",
    "mapping_validate_project_scope",
    "mapping_validate_all_for_project",
    # -----------------------------
    # data modelling (transformations)
    # -----------------------------
    "transformation_create",
    "transformation_get",
    "transformation_update",
    "transformation_delete",
    "transformation_list",
    "transformation_set_script",
    "transformation_mark_draft",
    "transformation_mark_finished",
    "transformation_link_mapping",
    "transformation_unlink_mapping",
    "transformation_list_links",
    "transformation_clear_links",
    "transformation_script_stub",
    "br_health",
    "br_session_create",
    "br_session_close",
    "br_navigate",
    "br_click",
    "br_type",
    "br_wait",
    "br_extract",
    "br_screenshot",

    "life_health",
    "life_user_create",
    "life_user_profile_upsert",
    "life_user_context",
    "life_goal_create",
    "life_goal_list",
    "life_exercise_search",
    "life_exercise_resolve",
    "life_workout_log_smart",
    "life_workout_list",
    "life_workout_get",
    "life_activity_log",
    "life_activity_list",
    "life_body_measurement_create",
    "life_health_overview",
    "life_program_create_full",
    "life_analytics_workout_summary",
    "life_context_llm_summary",
    "life_context_today_training",
}

LOOKUP_TOOLS = {
    # todo
    "todo_list_list", "todo_item_list", "todo_today", "todo_supplement_list",
    # pm
    "pm_project_list", "pm_project_get", "pm_project_get_full",
    "pm_epic_list", "pm_feature_list", "pm_workitem_list", "pm_task_list",
    # search
    "text_search", "code_search",
}
INTERNAL_TOOLS = {"ingest_status"}
MUTATION_TOOLS = {"text_update", "text_delete"}