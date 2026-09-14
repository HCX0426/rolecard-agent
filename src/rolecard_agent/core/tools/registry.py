"""Tool registry + two-stage filtering.

Stage 1: keep only tools belonging to ENABLED domains (plugin switch).
Stage 2: narrow to the current role's tool_whitelist BEFORE bind_tools is called,
         so the model never even sees a tool it is not allowed to call.
"""
