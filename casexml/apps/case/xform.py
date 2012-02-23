from casexml.apps.case import const

"""
Work on cases based on XForms. In our world XForms are special couch documents.
"""
from casexml.apps.case.models import CommCareCase
from couchdbkit.schema.properties_proxy import SchemaProperty
import logging
from couchdbkit.resource import ResourceNotFound
from casexml.apps.case.xml.parser import case_update_from_block


def get_or_update_cases(xformdoc):
    """
    Given an xform document, update any case blocks found within it,
    returning a dicitonary mapping the case ids affected to the
    couch case document objects
    """
    case_blocks = extract_case_blocks(xformdoc)
    cases_touched = {}
    for case_block in case_blocks:
        case_doc = get_or_update_model(case_block, xformdoc)
        if case_doc:
            case_doc.xform_ids.append(xformdoc.get_id)
            cases_touched[case_doc.case_id] = case_doc
        else:
            logging.error("Xform %s had a case block that wasn't able to create a case! This usually means it had a missing ID" % xformdoc.get_id)
    return cases_touched

def get_or_update_model(case_block, xformdoc):
    """
    Gets or updates an existing case, based on a block of data in a 
    submitted form.  Doesn't save anything.
    """
    
    case_update = case_update_from_block(case_block)
    try: 
        case_doc = CommCareCase.get(case_update.id)
        # some forms recycle case ids as other ids (like xform ids)
        # disallow that hard.
        if case_doc.doc_type != "CommCareCase":
            raise Exception("Bad case doc type! This usually means you are using a bad value for case_id.")
    except ResourceNotFound:
        case_doc = None
    
    if case_doc == None:
        case_doc = CommCareCase.from_case_update(case_update, xformdoc)
        return case_doc
    else:
        case_doc.update_from_case_update(case_update, xformdoc)
        return case_doc
        
def is_excluded(doc):
    # exclude anything matching a certain set of conditions from case processing.
    # as of today, the only things that meet these requirements are device logs.
    device_report_xmlns = "http://code.javarosa.org/devicereport"
    try: 
        return (hasattr(doc, "xmlns") and doc.xmlns == device_report_xmlns) or \
               ("@xmlns" in doc and doc["@xmlns"] == device_report_xmlns)
    except TypeError:
        return False # wasn't iterable, don't exclude

def extract_case_blocks(doc):
    """
    Extract all case blocks from a document, returning an array of dictionaries
    with the data in each case. 
    """
    if doc is None or is_excluded(doc): return []
    
    block_list = []
    if isinstance(doc, list):
        for item in doc: 
            block_list.extend(extract_case_blocks(item))
    else:
        try: 
            for key, value in doc.items():
                if const.CASE_TAG == key:
                    # we explicitly don't support nested cases yet, so no need
                    # to check further
                    # BUT, it could be a list
                    if isinstance(value, list):
                        for item in value:
                            block_list.append(item)
                    else: 
                        block_list.append(value) 
                else:
                    # recursive call
                    block_list.extend(extract_case_blocks(value))
        except AttributeError:
            # whoops, this wasn't a list or dictionary, 
            # an expected outcome in the recursive case.
            # Fall back to base case.
            return []
    
    # filter out anything without a case id property
    def _has_case_id(case_block):
        return const.CASE_TAG_ID in case_block or \
               "@%s" % const.CASE_TAG_ID in case_block
    return [block for block in block_list if _has_case_id(block)]