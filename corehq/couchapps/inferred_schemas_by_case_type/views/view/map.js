function(doc) {
    if (doc.doc_type === "InferredSchema") {
        emit([doc.domain, doc.doc_type, doc.case_type, doc.created_on], null);
    }
}
