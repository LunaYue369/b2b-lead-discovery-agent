"""全项目共用的类型定义 —— 整个 b2b-outreach 的词汇表。

所有层都 import 这个包，这个包不 import 任何层。

按主题分成八个模块：

    company.py    公司身份：两条数据说的是不是同一家公司
    signal.py     信号与候选主体，pipeline 的三段基数
    filtering.py  prefilter 的丢弃理由和丢弃记录
    scoring.py    打分，LLM 判的和代码算的分开
    query.py      query 本身：怎么来的、算哪一档、多久跑一次
    run.py        一次 run、一次 execution，以及全部计数
    lead.py       lead 状态和 Riley 的反馈类型
    contact.py    找联系方式：公司概况、查过的人、找到的邮箱电话、花的钱

这里统一 re-export，所以调用方一律写 `from models import CompanyRef`，
不用关心它住在哪个文件里。

两条硬约束（后面写代码时别破坏）：

1. 只依赖标准库。验收方式：scripts/check_isolation.py 用 ast 静态检查
   这个包里每个模块的 import。守住这条，Slack、数据库、LLM
   谁都漏不进词汇表。

2. 字段一律 required + fail-fast，不加 Optional + fallback。唯一例外是
   「外部数据真实缺失」的字段（LinkedIn 就是不给），那种地方允许 None，
   但不允许静默降级 —— 见 CompanyRef.identity_key。
"""

from models.company import (
    CompanyRef,
    IdentityUnresolvable,
    linkedin_company_slug,
    normalize_company_name,
    normalize_domain,
)
from models.contact import (
    NO_CONTACTS,
    AuthorKind,
    AuthorRef,
    CardContacts,
    CardEmail,
    CardPerson,
    CardRelation,
    CompanyProfile,
    ContactChannel,
    ContactSource,
    CurrentJob,
    DomainWrite,
    FoundContact,
    LeadForContacts,
    PersonLookup,
    SavedContact,
    SavedPersonLookup,
    ScrapedPerson,
    SpendPurpose,
)
from models.filtering import (
    CandidateDrop,
    CandidateDropReason,
    SignalDrop,
    SignalDropReason,
)
from models.lead import (
    CardEvidence,
    CardQuote,
    CardSource,
    DraftEvidence,
    DraftSource,
    FeedbackType,
    LeadCard,
    LeadHit,
    LeadState,
    LeadStatus,
    SavedLead,
    TriageVerdict,
)
from models.query import (
    POSTED_WITHIN_TEXT,
    POSTED_WITHIN_VALUES,
    KnownQuery,
    QueryPool,
    QuerySource,
    QuerySpec,
    QueryStats,
    QueryTotals,
    normalize_query_text,
    validate_filters,
)
from models.run import (
    DiscoveryHit,
    DiscoveryResult,
    ExecutionCounters,
    ExecutionStatus,
    QueryOutcome,
    RunStatus,
    RunType,
)
from models.scoring import (
    Assessment,
    AssessmentProblem,
    DeterministicScores,
    LlmAssessment,
    find_quote,
    locate_quote,
    unverified_quotes,
)
from models.signal import (
    ActorType,
    Candidate,
    CompanyCluster,
    LeadType,
    RawItem,
    RawSignal,
    RepostedFrom,
    Signal,
    SourceType,
)

__all__ = [
    "ActorType",
    "Assessment",
    "AssessmentProblem",
    "Candidate",
    "CandidateDrop",
    "CandidateDropReason",
    "CompanyCluster",
    "CompanyRef",
    "AuthorKind",
    "AuthorRef",
    "CardContacts",
    "CardEmail",
    "CardPerson",
    "CardRelation",
    "CompanyProfile",
    "ContactChannel",
    "ContactSource",
    "CurrentJob",
    "DomainWrite",
    "FoundContact",
    "LeadForContacts",
    "NO_CONTACTS",
    "PersonLookup",
    "SavedContact",
    "SavedPersonLookup",
    "ScrapedPerson",
    "SpendPurpose",
    "DeterministicScores",
    "DiscoveryHit",
    "DiscoveryResult",
    "ExecutionCounters",
    "ExecutionStatus",
    "FeedbackType",
    "IdentityUnresolvable",
    "CardEvidence",
    "CardQuote",
    "CardSource",
    "DraftEvidence",
    "DraftSource",
    "LeadCard",
    "LeadHit",
    "LeadState",
    "LeadStatus",
    "LeadType",
    "LlmAssessment",
    "QueryOutcome",
    "KnownQuery",
    "POSTED_WITHIN_TEXT",
    "POSTED_WITHIN_VALUES",
    "QueryPool",
    "QuerySource",
    "QuerySpec",
    "QueryStats",
    "QueryTotals",
    "RawItem",
    "RawSignal",
    "RepostedFrom",
    "RunStatus",
    "RunType",
    "SavedLead",
    "Signal",
    "SignalDrop",
    "SignalDropReason",
    "SourceType",
    "TriageVerdict",
    "linkedin_company_slug",
    "find_quote",
    "locate_quote",
    "normalize_company_name",
    "normalize_domain",
    "normalize_query_text",
    "unverified_quotes",
    "validate_filters",
]
