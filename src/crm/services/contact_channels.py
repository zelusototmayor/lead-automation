"""Read operational person and company channels without copying general data into people."""
from sqlalchemy import String,and_,case,exists,func,or_,select
from src.crm.persistence.models import Contact,Evidence,Lead,SourceIdentity

def display_phone_columns():
    """Keep the selected person's phone; otherwise expose one general company number."""
    selected = func.coalesce(
        case((func.length(func.trim(Contact.phone)) > 0, Contact.phone)),
        case((func.length(func.trim(Lead.contact_phone)) > 0, Lead.contact_phone)),
    )
    company_contact = Contact.__table__.alias("company_phone_contact")
    number = func.trim(company_contact.c.phone)
    # Named contacts qualify only with a reviewed company-phone source assertion.
    reviewed_company_phone = exists(
        select(Evidence.id).join(
            SourceIdentity,
            and_(SourceIdentity.id == Evidence.source_identity_id,
                 SourceIdentity.workspace_id == Evidence.workspace_id),
        ).where(
            Evidence.workspace_id == Lead.workspace_id,
            Evidence.account_id == Lead.account_id,
            Evidence.evidence_type == "sheet_cell",
            SourceIdentity.source_system == "agent",
            SourceIdentity.entity_kind == "account",
            SourceIdentity.source_scope == "company-phone/verified-v1",
            SourceIdentity.canonical_entity_type == "account",
            SourceIdentity.canonical_entity_id == Lead.account_id,
            Evidence.metadata_json.contains(
                {"schema_version": 1, "reviewed": True, "phone_kind": "company"}
            ),
            Evidence.metadata_json["source_contact_id"].as_string() == company_contact.c.id.cast(String),
            Evidence.metadata_json["phone_normalized"].as_string()
                == func.regexp_replace(number, "[^0-9]", "", "g"),
        ).correlate(Lead, company_contact)
    )
    eligible = (
        company_contact.c.workspace_id == Lead.workspace_id,
        company_contact.c.account_id == Lead.account_id,
        company_contact.c.status == "active",
        or_(company_contact.c.full_name.is_(None),
            func.trim(company_contact.c.full_name) == "", reviewed_company_phone),
        func.length(number) > 0,
    )
    def unique_value(value):
        return (select(value).where(*eligible)
                .having(func.count(func.distinct(number)) == 1)
                .correlate(Lead).scalar_subquery())
    company_phone = unique_value(func.min(number))
    company_source = unique_value(func.min(company_contact.c.id.cast(String)))
    use_company = or_(and_(selected.is_(None), company_phone.is_not(None)),
        and_(selected.is_not(None), Contact.id.is_not(None),
             or_(Contact.full_name.is_(None), func.trim(Contact.full_name)=='')))
    return (
        func.coalesce(selected, company_phone).label("phone"),
        case((use_company, "company"), else_=None).label("phone_kind"),
        case((selected.is_not(None), Contact.id.cast(String)), else_=company_source).label("phone_source_contact_id"),
    )



def display_email_columns():
    selected=func.coalesce(Contact.primary_email,Lead.contact_email)
    general=Contact.__table__.alias('company_email_contact')
    address=func.lower(func.trim(general.c.primary_email.cast(String)))
    eligible=(general.c.workspace_id==Lead.workspace_id,general.c.account_id==Lead.account_id,
        general.c.status=='active',or_(general.c.full_name.is_(None),func.trim(general.c.full_name)==''),
        func.length(address)>0)
    def unique(value):
        return select(value).where(*eligible).having(func.count(func.distinct(address))==1).correlate(Lead).scalar_subquery()
    email=unique(func.min(address));source=unique(func.min(general.c.id.cast(String)))
    company=or_(and_(selected.is_(None),email.is_not(None)),
        and_(selected.is_not(None),Contact.id.is_not(None),or_(Contact.full_name.is_(None),func.trim(Contact.full_name)=='')))
    return (func.coalesce(selected,email).label('email'),
        case((company,'company'),else_=None).label('email_kind'),
        case((selected.is_not(None),Contact.id.cast(String)),else_=source).label('email_source_contact_id'))


def operational_context(session,workspace_id,lead_id):
    row=session.execute(select(Lead.contact_id,*display_email_columns(),*display_phone_columns())
        .outerjoin(Contact,(Contact.workspace_id==Lead.workspace_id)&(Contact.id==Lead.contact_id))
        .where(Lead.workspace_id==workspace_id,Lead.id==lead_id)).one()
    return {'operational_contact_id':str(row.contact_id) if row.contact_id else None,
        **{f'operational_{field}':str(getattr(row,field)) if getattr(row,field) is not None else None
           for field in ['email','phone','email_source_contact_id','phone_source_contact_id']},
        **{f'operational_{field}_kind':(getattr(row,field+'_kind') or 'person') if getattr(row,field) else None
           for field in ['email','phone']}}
