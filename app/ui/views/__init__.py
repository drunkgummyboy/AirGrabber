"""One module per page."""

from app.ui.views.base import BaseView
from app.ui.views.calendar_view import CalendarView
from app.ui.views.discover_view import DiscoverView
from app.ui.views.movie_view import MovieView
from app.ui.views.tracked_view import TrackedView

__all__ = ["BaseView", "CalendarView", "DiscoverView", "MovieView", "TrackedView"]
