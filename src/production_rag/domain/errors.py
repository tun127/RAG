class RagError(Exception):
    """可以向调用者解释的业务/依赖错误。"""


class DependencyError(RagError):
    pass


class NoKnowledgeBase(RagError):
    pass


class BudgetExceeded(RagError):
    pass


class Busy(RagError):
    pass


class PublishConflict(RagError):
    pass
