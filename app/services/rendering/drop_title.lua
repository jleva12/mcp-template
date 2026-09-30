-- Pandoc turns the HTML <title> into a title paragraph at the top of the document. Templates are
-- written for PDF, which doesn't show the <title>, and usually repeat it in a heading, so drop it.
function Meta(meta)
  meta.title = nil
  return meta
end
