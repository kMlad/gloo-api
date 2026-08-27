from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)

from app.auth import AuthenticatedUser, require_authenticated_user
from app.dependencies import get_table_service
from app.tables.email_enrichment import EmailEnrichmentUnavailableError
from app.tables.sheriff import SheriffUnavailableError
from app.tables.csv_export import content_disposition_attachment
from app.tables.csv_import import CsvImportError
from app.tables.schemas import (
    SheriffExpandRequest,
    SheriffExpandResponse,
    SheriffOptionsResponse,
    SheriffRunCreate,
    SheriffRunResponse,
    ColumnCreate,
    ColumnOrderUpdate,
    ColumnResponse,
    ColumnUpdate,
    RowCreate,
    RowListResponse,
    RowResponse,
    RowUpdate,
    TableCreate,
    TableFiltersUpdate,
    TableListResponse,
    TableResponse,
    TableUpdate,
)
from app.tables.service import (
    TableConflictError,
    TableNotFoundError,
    TableService,
    TableValidationError,
)

router = APIRouter(
    prefix="/api/v1/tables",
    tags=["tables"],
    dependencies=[Depends(require_authenticated_user)],
)

ServiceDependency = Annotated[TableService, Depends(get_table_service)]
UserDependency = Annotated[AuthenticatedUser, Depends(require_authenticated_user)]


def _map_table_error(error: Exception) -> HTTPException:
    if isinstance(error, TableNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, TableConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, (TableValidationError, CsvImportError)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        )
    if isinstance(error, (SheriffUnavailableError, EmailEnrichmentUnavailableError)):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        )
    raise error


@router.get("", response_model=TableListResponse)
async def list_tables(service: ServiceDependency) -> dict[str, Any]:
    return await service.list_tables()


@router.post("", response_model=TableResponse, status_code=status.HTTP_201_CREATED)
async def create_table(
    payload: TableCreate,
    service: ServiceDependency,
    user: UserDependency,
) -> dict[str, Any]:
    try:
        return await service.create_table(payload, user.id)
    except (TableNotFoundError, TableConflictError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.post(
    "/imports", response_model=TableResponse, status_code=status.HTTP_201_CREATED
)
async def import_table_csv(
    service: ServiceDependency,
    user: UserDependency,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> dict[str, Any]:
    content = await file.read()
    try:
        return await service.import_csv(
            content=content,
            filename=file.filename,
            name=name,
            created_by=user.id,
        )
    except (
        TableNotFoundError,
        TableConflictError,
        TableValidationError,
        CsvImportError,
    ) as error:
        raise _map_table_error(error) from error


@router.get("/{table_id}", response_model=TableResponse)
async def get_table(table_id: UUID, service: ServiceDependency) -> dict[str, Any]:
    try:
        return await service.get_table(str(table_id))
    except TableNotFoundError as error:
        raise _map_table_error(error) from error


@router.patch("/{table_id}", response_model=TableResponse)
async def update_table(
    table_id: UUID, payload: TableUpdate, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.update_table(str(table_id), payload)
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.put("/{table_id}/filters", response_model=TableResponse)
async def replace_table_filters(
    table_id: UUID, payload: TableFiltersUpdate, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.replace_filters(str(table_id), payload)
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.delete("/{table_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_table(table_id: UUID, service: ServiceDependency) -> Response:
    try:
        await service.delete_table(str(table_id))
    except TableNotFoundError as error:
        raise _map_table_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{table_id}/columns",
    response_model=ColumnResponse | TableResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_column(
    table_id: UUID, payload: ColumnCreate, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.add_column(str(table_id), payload)
    except (
        TableNotFoundError,
        TableConflictError,
        TableValidationError,
        SheriffUnavailableError,
    ) as error:
        raise _map_table_error(error) from error


@router.patch(
    "/{table_id}/columns/{column_id}",
    response_model=ColumnResponse | TableResponse,
)
async def update_column(
    table_id: UUID,
    column_id: UUID,
    payload: ColumnUpdate,
    service: ServiceDependency,
) -> dict[str, Any]:
    try:
        return await service.update_column(str(table_id), str(column_id), payload)
    except (TableNotFoundError, TableConflictError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.put("/{table_id}/columns/order", response_model=TableResponse)
async def reorder_columns(
    table_id: UUID, payload: ColumnOrderUpdate, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.reorder_columns(str(table_id), payload.column_ids)
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.delete(
    "/{table_id}/columns/{column_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_column(
    table_id: UUID, column_id: UUID, service: ServiceDependency
) -> Response:
    try:
        await service.delete_column(str(table_id), str(column_id))
    except TableNotFoundError as error:
        raise _map_table_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{table_id}/sheriff/options",
    response_model=SheriffOptionsResponse,
)
async def get_sheriff_options(
    table_id: UUID,
    service: ServiceDependency,
) -> dict[str, Any]:
    try:
        return await service.get_sheriff_options(str(table_id))
    except TableNotFoundError as error:
        raise _map_table_error(error) from error


@router.post(
    "/{table_id}/sheriff/prompts/expand",
    response_model=SheriffExpandResponse,
)
async def expand_sheriff_prompt(
    table_id: UUID,
    payload: SheriffExpandRequest,
    service: ServiceDependency,
) -> dict[str, Any]:
    try:
        return await service.expand_sheriff_prompt(str(table_id), payload)
    except (
        TableNotFoundError,
        TableValidationError,
        SheriffUnavailableError,
    ) as error:
        raise _map_table_error(error) from error


@router.post(
    "/{table_id}/columns/{column_id}/runs",
    response_model=SheriffRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_column_run(
    table_id: UUID,
    column_id: UUID,
    service: ServiceDependency,
    user: UserDependency,
    background_tasks: BackgroundTasks,
    payload: SheriffRunCreate | None = None,
) -> dict[str, Any]:
    request = payload or SheriffRunCreate()
    try:
        column = await service.get_column(str(table_id), str(column_id))
        column_type = column["type"]
        if column_type == "sheriff":
            run = await service.start_sheriff_run(
                str(table_id),
                str(column_id),
                request,
                created_by=user.id,
            )
            background_tasks.add_task(service.execute_sheriff_run, str(run["id"]))
            return run
        if column_type == "email_enrichment":
            run = await service.start_email_enrichment_run(
                str(table_id),
                str(column_id),
                request,
                created_by=user.id,
            )
            background_tasks.add_task(
                service.execute_email_enrichment_run, str(run["id"])
            )
            return run
        if column_type == "email_validation":
            run = await service.start_email_validation_run(
                str(table_id),
                str(column_id),
                request,
                created_by=user.id,
            )
            background_tasks.add_task(
                service.execute_email_validation_run, str(run["id"])
            )
            return run
        raise TableValidationError(
            "Runs are only supported on sheriff, email_enrichment, "
            "and email_validation columns"
        )
    except (
        TableNotFoundError,
        TableValidationError,
        SheriffUnavailableError,
        EmailEnrichmentUnavailableError,
    ) as error:
        raise _map_table_error(error) from error


@router.get(
    "/{table_id}/columns/{column_id}/runs/{run_id}",
    response_model=SheriffRunResponse,
)
async def get_column_run(
    table_id: UUID,
    column_id: UUID,
    run_id: UUID,
    service: ServiceDependency,
) -> dict[str, Any]:
    try:
        return await service.get_column_run(
            str(table_id), str(column_id), str(run_id)
        )
    except TableNotFoundError as error:
        raise _map_table_error(error) from error


@router.get("/{table_id}/export")
async def export_table(
    table_id: UUID,
    service: ServiceDependency,
    sort_column_id: UUID | None = Query(default=None),
    sort_direction: Literal["asc", "desc"] = Query(default="asc"),
) -> Response:
    try:
        filename, content = await service.export_csv(
            str(table_id),
            sort_column_id=None if sort_column_id is None else str(sort_column_id),
            sort_direction=sort_direction,
        )
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition_attachment(filename),
            "Cache-Control": "no-store",
        },
    )


@router.get("/{table_id}/rows", response_model=RowListResponse)
async def list_rows(
    table_id: UUID,
    service: ServiceDependency,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        return await service.list_rows(str(table_id), limit=limit, offset=offset)
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.post(
    "/{table_id}/rows", response_model=RowResponse, status_code=status.HTTP_201_CREATED
)
async def add_row(
    table_id: UUID,
    service: ServiceDependency,
    payload: RowCreate | None = None,
) -> dict[str, Any]:
    try:
        return await service.add_row(str(table_id), payload or RowCreate())
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.patch("/{table_id}/rows/{row_id}", response_model=RowResponse)
async def update_row(
    table_id: UUID,
    row_id: UUID,
    payload: RowUpdate,
    service: ServiceDependency,
) -> dict[str, Any]:
    try:
        return await service.update_row(str(table_id), str(row_id), payload)
    except (TableNotFoundError, TableValidationError) as error:
        raise _map_table_error(error) from error


@router.delete("/{table_id}/rows/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_row(
    table_id: UUID, row_id: UUID, service: ServiceDependency
) -> Response:
    try:
        await service.delete_row(str(table_id), str(row_id))
    except TableNotFoundError as error:
        raise _map_table_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
