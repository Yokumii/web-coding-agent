import {useLayoutEffect, useRef} from "react";
import {mountFileUpload} from "./upload.mjs";

// Keep configure stable (useCallback); return only host bindings/options.
// This empty subtree belongs exclusively to the reference component.
export default function EditComponent({configure, apiRef, className}) {
  const root = useRef(null);
  useLayoutEffect(() => {
    const options = configure(root.current);
    const component = mountFileUpload(options);
    if (apiRef) apiRef.current = component;
    return () => {
      component.destroy();
      if (apiRef?.current === component) apiRef.current = null;
    };
  }, [configure, apiRef]);
  return <div ref={root} className={className} />;
}
