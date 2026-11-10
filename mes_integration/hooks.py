app_name = "mes_integration"
app_title = "Mes Integration"
app_publisher = "yuewei"
app_description = "connect to mes"
app_email = "308642281@qq.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "mes_integration",
# 		"logo": "/assets/mes_integration/logo.png",
# 		"title": "Mes Integration",
# 		"route": "/mes_integration",
# 		"has_permission": "mes_integration.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/mes_integration/css/mes_integration.css"
# app_include_js = "/assets/mes_integration/js/mes_integration.js"
app_include_js = "/assets/mes_integration/js/system_log_sidebar.js"

# include js, css files in header of web template
# web_include_css = "/assets/mes_integration/css/mes_integration.css"
# web_include_js = "/assets/mes_integration/js/mes_integration.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "mes_integration/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

boot_session = ["mes_integration.boot.boot_session"]

# include js in page
# page_js = {"page" : "public/js/file.js"}

doctype_js = {
	"Delivery Note": "public/js/delivery_note.js",
	"Material Request": "public/js/material_request.js",
	"Stock Entry": "public/js/stock_entry.js",
}
doctype_list_js = {
	"Delivery Note": "public/js/delivery_note_list.js",
	"Material Request": "public/js/material_request_list.js",
	"Stock Entry": "public/js/stock_entry_list.js",
	"MES Integration Log": "public/js/mes_integration_log_list.js",
	"MES Material Request Task": "public/js/mes_material_request_task_list.js",
}
doctype_css = {"Stock Entry": "public/css/stock_entry.css"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "mes_integration/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "mes_integration.utils.jinja_methods",
# 	"filters": "mes_integration.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "mes_integration.install.before_install"
# after_install = "mes_integration.install.after_install"
after_install = "mes_integration.setup.after_install"
after_migrate = "mes_integration.setup.after_migrate"

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"mes_integration.mes_integration.material_request.recover_material_request_tasks"
		]
	}
}

# Uninstallation
# ------------

# before_uninstall = "mes_integration.uninstall.before_uninstall"
# after_uninstall = "mes_integration.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "mes_integration.utils.before_app_install"
# after_app_install = "mes_integration.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "mes_integration.utils.before_app_uninstall"
# after_app_uninstall = "mes_integration.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "mes_integration.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "mes_integration.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }
doc_events = {
	"Delivery Note": {
		"validate": "mes_integration.mes_integration.delivery_note.set_delivery_readiness_status",
		"on_submit": "mes_integration.mes_integration.delivery_note.set_delivered_readiness_status",
		"on_cancel": "mes_integration.mes_integration.delivery_note.clear_delivery_readiness_status",
	},
	"Material Request": {
		"before_insert": "mes_integration.mes_integration.material_request.set_material_request_source",
		"validate": "mes_integration.mes_integration.material_request.validate_item_details",
		"after_insert": "mes_integration.mes_integration.integration_log.log_inbound_material_request",
	},
	"Stock Entry": {
		"validate": "mes_integration.mes_integration.stock_entry.validate_reviewed_mes_receipt_identity",
		"on_submit": [
			"mes_integration.mes_integration.integration_log.log_inbound_stock_entry",
			"mes_integration.mes_integration.stock_entry.update_material_request_transferred_qty",
			"mes_integration.mes_integration.stock_entry.notify_mes_stock_entry_status",
		],
		"on_cancel": [
			"mes_integration.mes_integration.stock_entry.update_material_request_transferred_qty",
			"mes_integration.mes_integration.stock_entry.notify_mes_stock_entry_status",
		],
	},
}

extend_doctype_class = {
	"Material Request": [
		"mes_integration.mes_integration.material_request.MESMaterialRequestPerformanceMixin"
	],
}

default_log_clearing_doctypes = {
	"MES Integration Log": 180,
	"MES Material Request Task": 30,
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"mes_integration.tasks.all"
# 	],
# 	"daily": [
# 		"mes_integration.tasks.daily"
# 	],
# 	"hourly": [
# 		"mes_integration.tasks.hourly"
# 	],
# 	"weekly": [
# 		"mes_integration.tasks.weekly"
# 	],
# 	"monthly": [
# 		"mes_integration.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "mes_integration.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"ToDo": ["app.overrides.CustomToDo"]
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "mes_integration.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "mes_integration.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["mes_integration.utils.before_request"]
# after_request = ["mes_integration.utils.after_request"]

# Job Events
# ----------
# before_job = ["mes_integration.utils.before_job"]
# after_job = ["mes_integration.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"mes_integration.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
