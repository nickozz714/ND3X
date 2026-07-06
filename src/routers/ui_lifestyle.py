from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from routers._mcp_proxy import mcp_proxy_call, mcp_proxy_health
from component.config import settings
from services.audit_service import AuditService
from services.mcp.mcp_client import MCPClient

router = APIRouter(prefix="/ui/lifestyle", tags=["ui-lifestyle"])
mcp = MCPClient(mcp_url=settings.MCP_URL, bearer=settings.MCP_BEARER)
audit = AuditService()

SERVICE_NAME = "lifestyle"
HEALTH_TOOL = "life_health"


async def _call(tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await mcp_proxy_call(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=tool_name,
        payload=payload,
    )



# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------

class UserCreateRequest(BaseModel):
    external_id: str
    email: Optional[str] = None
    username: Optional[str] = None


class UserProfileUpsertRequest(BaseModel):
    user_id: int
    first_name: str
    birth_date: str
    sex: str
    height_cm: float
    timezone: str = "Europe/Amsterdam"
    locale: str = "nl-NL"
    unit_system: str = "metric"
    profile_completed: bool = True
    preferred_workout_days: Optional[List[str]] = None
    preferred_session_duration_minutes: Optional[int] = None
    preferred_training_style: Optional[str] = None
    preferred_location_type: Optional[str] = None


class GoalCreateRequest(BaseModel):
    user_id: int
    goal_type: str
    title: str
    description: str = ""
    priority: int = 1
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = "active"


class ExerciseSearchRequest(BaseModel):
    q: Optional[str] = None
    muscle_group_id: Optional[int] = None
    movement_pattern_id: Optional[int] = None
    modality: Optional[str] = None
    location_type: Optional[str] = None
    limit: int = 25
    offset: int = 0


class WorkoutSetInput(BaseModel):
    set_number: int
    reps: Optional[int] = None
    weight_kg: Optional[float] = None
    duration_seconds: Optional[int] = None
    distance_meters: Optional[float] = None
    rest_seconds: Optional[int] = None
    rpe: Optional[int] = None
    completed: bool = True


class WorkoutExerciseInput(BaseModel):
    exercise_definition_id: Optional[int] = None
    name: Optional[str] = None
    sequence_order: Optional[int] = None
    notes: str = ""
    modality: str = "strength"
    primary_muscle_group_id: Optional[int] = None
    movement_pattern_id: Optional[int] = None
    difficulty_level: str = "intermediate"
    default_tracking_type: str = "reps_weight"
    compound: bool = False
    sets: List[WorkoutSetInput] = Field(default_factory=list)


class WorkoutLogSmartRequest(BaseModel):
    user_id: int
    session_name: str
    started_at: str
    ended_at: str
    session_type: str
    location_type: str
    exercises: List[WorkoutExerciseInput]
    notes: str = ""
    duration_minutes: Optional[int] = None
    perceived_intensity: Optional[int] = None
    completion_status: str = "completed"
    auto_create_missing_exercises: bool = True
    source: Optional[str] = None
    calories_burned_estimated: Optional[float] = None


class BodyMeasurementCreateRequest(BaseModel):
    user_id: int
    measured_at: str
    weight_kg: float
    notes: str = ""
    source: Optional[str] = None


class ProgramExerciseInput(BaseModel):
    exercise_definition_id: int
    sequence_order: int
    target_sets: Optional[int] = None
    target_reps_min: Optional[int] = None
    target_reps_max: Optional[int] = None
    target_rpe: Optional[int] = None
    notes: Optional[str] = None


class ProgramDayInput(BaseModel):
    day_number: int
    title: str
    focus: Optional[str] = None
    intended_duration_minutes: Optional[int] = None
    exercises: List[ProgramExerciseInput] = Field(default_factory=list)


class ProgramCreateFullRequest(BaseModel):
    user_id: int
    name: str
    description: str
    goal_type: str
    level: str
    duration_weeks: int
    sessions_per_week: int
    status: str = "active"
    days: List[ProgramDayInput] = Field(default_factory=list)

class ExerciseModality(str, Enum):
    strength = "strength"
    cardio = "cardio"
    mobility = "mobility"
    recovery = "recovery"
    mixed = "mixed"
    sports_specific = "sports_specific"


class ExerciseDifficulty(str, Enum):
    beginner = "beginner"
    novice = "novice"
    intermediate = "intermediate"
    advanced = "advanced"
    elite = "elite"


class ExerciseTrackingType(str, Enum):
    reps_weight = "reps_weight"
    reps_only = "reps_only"
    weight_only = "weight_only"
    duration = "duration"
    distance = "distance"
    duration_distance = "duration_distance"

class LocationType(str, Enum):
    gym = 'gym'
    home = 'home'
    outdoor = 'outdoor'
    travel = 'travel'
    mixed = 'mixed'

class ExerciseDefinitionBase(BaseModel):
    name: str
    slug: str
    description: str | None = None
    modality: ExerciseModality = ExerciseModality.strength
    primary_muscle_group_id: int | None = None
    movement_pattern_id: int | None = None
    difficulty_level: ExerciseDifficulty | None = None
    default_tracking_type: ExerciseTrackingType| None = None
    location_type: LocationType | None = None
    equipment_required: str | None = None
    compound: bool = False
    unilateral: bool = False
    is_active: bool = True

# ---------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------

@router.get("/health")
async def ui_life_health() -> Dict[str, Any]:
    return await mcp_proxy_health(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=HEALTH_TOOL,
    )


# ---------------------------------------------------------------------
# User / profile
# ---------------------------------------------------------------------

@router.post("/users")
async def ui_life_user_create(body: UserCreateRequest) -> Dict[str, Any]:
    return await _call("life_user_create", body.model_dump())


@router.post("/users/profile")
async def ui_life_user_profile_upsert(body: UserProfileUpsertRequest) -> Dict[str, Any]:
    return await _call("life_user_profile_upsert", body.model_dump())


@router.get("/users/{user_id}/context")
async def ui_life_user_context(user_id: int) -> Dict[str, Any]:
    return await _call("life_user_context", {"user_id": user_id})


# ---------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------

@router.post("/goals")
async def ui_life_goal_create(body: GoalCreateRequest) -> Dict[str, Any]:
    return await _call("life_goal_create", body.model_dump())


@router.get("/users/{user_id}/goals")
async def ui_life_goal_list(user_id: int) -> Dict[str, Any]:
    return await _call("life_goal_list", {"user_id": user_id})


# ---------------------------------------------------------------------
# Exercises
# ---------------------------------------------------------------------

@router.get("/exercises/search")
async def ui_life_exercise_search(
    q: Optional[str] = Query(default=None),
    muscle_group_name: Optional[str] = Query(default=None),
    movement_pattern_name: Optional[str] = Query(default=None),
    modality: Optional[str] = Query(default=None),
    location_type: Optional[str] = Query(default=None),
    limit: int = Query(default=25),
    offset: int = Query(default=0),
) -> Dict[str, Any]:
    return await _call(
        "life_exercise_search",
        {
            "q": q,
            "muscle_group_name": muscle_group_name,
            "movement_pattern_name": movement_pattern_name,
            "modality": modality,
            "location_type": location_type,
            "limit": limit,
            "offset": offset,
        },
    )


# ---------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------

@router.post("/workouts/log-smart")
async def ui_life_workout_log_smart(body: WorkoutLogSmartRequest) -> Dict[str, Any]:
    return await _call("life_workout_log_smart", body.model_dump())


@router.get("/users/{user_id}/workouts")
async def ui_life_workout_list(
    user_id: int,
    limit: int = Query(default=20),
    offset: int = Query(default=0),
) -> Dict[str, Any]:
    return await _call(
        "life_workout_list",
        {
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/workouts/{session_id}")
async def ui_life_workout_get(session_id: int) -> Dict[str, Any]:
    return await _call("life_workout_get", {"session_id": session_id})

@router.delete("/workouts/{session_id}")
async def ui_life_workout_delete(session_id: int) -> Dict[str, Any]:
    return await _call("life_workout_delete", {"session_id": session_id})
# ---------------------------------------------------------------------
# Body / health
# ---------------------------------------------------------------------

@router.post("/body-measurements")
async def ui_life_body_measurement_create(body: BodyMeasurementCreateRequest) -> Dict[str, Any]:
    return await _call("life_body_measurement_create", body.model_dump())


@router.get("/users/{user_id}/health-overview")
async def ui_life_health_overview(user_id: int) -> Dict[str, Any]:
    return await _call("life_health_overview", {"user_id": user_id})


# ---------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------

@router.post("/programs")
async def ui_life_program_create_full(body: ProgramCreateFullRequest) -> Dict[str, Any]:
    return await _call("life_program_create_full", body.model_dump())


# ---------------------------------------------------------------------
# Analytics / context
# ---------------------------------------------------------------------

@router.get("/users/{user_id}/analytics/workout-summary")
async def ui_life_analytics_workout_summary(
    user_id: int,
    days: int = Query(default=30),
) -> Dict[str, Any]:
    return await _call(
        "life_analytics_workout_summary",
        {
            "user_id": user_id,
            "days": days,
        },
    )


@router.get("/users/{user_id}/context/llm-summary")
async def ui_life_context_llm_summary(user_id: int) -> Dict[str, Any]:
    return await _call("life_context_llm_summary", {"user_id": user_id})


@router.get("/users/{user_id}/context/today-training")
async def ui_life_context_today_training(user_id: int) -> Dict[str, Any]:
    return await _call("life_context_today_training", {"user_id": user_id})

@router.get("/exercise-definitions")
async def ui_life_exercise_definitions_list():
    return await _call("life_get_exercises")

@router.get("/users/{user_id}/activity-sessions")
async def ui_life_activity_sessions_list(user_id: int) -> Dict[str, Any]:
    return await _call("life_activity_list", {"user_id": user_id})

@router.post("/users/{user_id}/activity-sessions")
async def ui_life_activity_sessions_create(
        user_id: int,
        activity_type: str,
        started_at: str,
        ended_at: str,
        activity_id: Optional[int] = None,
        distance_meters: Optional[float] = None,
        calories_burned: Optional[float] = None,
        avg_heart_rate: Optional[float] = None,
        is_update: bool = False,
        notes: str = "",
) -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "activity_type": activity_type,
        "started_at": started_at,
        "ended_at": ended_at,
        "activity_id": activity_id,
        "distance_meters": distance_meters,
        "calories_burned": calories_burned,
        "avg_heart_rate": avg_heart_rate,
        "notes": notes,
        "is_update": is_update,
    }
    return await _call("life_activity_log", payload)

@router.delete("/users/{user_id}/activity-sessions/{activity_id}")
async def ui_life_activity_sessions_delete(
        user_id: int,
        activity_id: int,
):
    return await _call("life_delete_activity", {"user_id": user_id, "activity_id": activity_id})
