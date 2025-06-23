import { type SyntaxNode, Tree } from "@lezer/common"
import { type ChangeSpec, countColumn, EditorSelection, EditorState, Line, SelectionRange, type StateCommand, Text, Transaction } from "@codemirror/state"
import { indentUnit, syntaxTree } from "@codemirror/language"
import { markdownLanguage } from "./language";
import { linesInRange, getChildren, intersectsRange, getIntersectionNodes, moveRangeDelete, moveRangeInsert } from './codemirror-utils';


type CommandArg = {state: EditorState, dispatch: (tr: Transaction) => void};


class Context {
  constructor(
    readonly node: SyntaxNode,
    readonly from: number,
    readonly to: number,
    readonly spaceBefore: string,
    readonly spaceAfter: string,
    readonly type: string,
    readonly item: SyntaxNode|null
  ) {
  }

  blank(maxWidth: number|null = null, trailing = true) {
    let result = this.spaceBefore;
    if (this.node.name === "blockQuote") { 
      result += ">";
    }
    if (maxWidth !== null) {
      while (result.length < maxWidth) {
        result += " ";
      }
      return result;
    } else {
      for (let i = this.to - this.from - result.length - this.spaceAfter.length; i > 0; i--) {
        result += " ";
      }
      return result + (trailing ? this.spaceAfter : "");
    }
  }

  marker(doc: Text, add: number) {
    let number = this.node.name == "listOrdered" ? String((+itemNumber(this.item!, doc)![2]! + add)) : ""
    return this.spaceBefore + number + this.type + this.spaceAfter
  }
}

function getContext(selectedNode: SyntaxNode, doc: Text) {
  let nodes = [] as SyntaxNode[]
  for (let cur: SyntaxNode|null = selectedNode; cur && cur.name != "document"; cur = cur.parent) {
    if (cur.name == "listItem" || cur.name == "blockQuote")
      nodes.push(cur)
  }
  let context = [] as Context[];
  for (let i = nodes.length - 1; i >= 0; i--) {
    let node = nodes[i]!, match;
    let line = doc.lineAt(node.from), startPos = node.from - line.from;
    if (node.name == "blockQuote" && (match = /^ *>( ?)/.exec(line.text.slice(startPos)))) {
      context.push(new Context(node, startPos, startPos + match[0].length, "", match[1]!, ">", null));
    } else if (node.name == "listItem" && node.parent!.name == "listOrdered" &&
               (match = /^( *)\d+([.)])( *)/.exec(line.text.slice(startPos)))) {
      let after = match[3]!, len = match[0].length
      if (after.length >= 4) { after = after.slice(0, after.length - 4); len -= 4 }
      context.push(new Context(node.parent!, startPos, startPos + len, match[1]!, after, match[2]!, node));
    } else if (node.name == "listItem" && node.parent!.name == "listUnordered" &&
               (match = /^( *)([-+*])( {1,4}\[[ xX]\])?( +)/.exec(line.text.slice(startPos)))) {
      let after = match[4]!, len = match[0].length
      if (after.length > 4) { after = after.slice(0, after.length - 4); len -= 4 }
      let type = match[2]!
      if (match[3]) type += match[3].replace(/[xX]/, ' ')
      context.push(new Context(node.parent!, startPos, startPos + len, match[1]!, after, type, node));
    }
  }
  return context
}

function itemNumber(item: SyntaxNode, doc: Text) {
  return /^(\s*)(\d+)(?=[.)])/.exec(doc.sliceString(item.from, item.from + 10))
}

function renumberList(after: SyntaxNode, doc: Text, changes: ChangeSpec[], offset = 0) {
  for (let prev = -1, node = after;;) {
    if (node.name == "listItem") {
      let m = itemNumber(node, doc)!
      let number = +m[2]!
      if (prev >= 0) {
        if (number != prev + 1) return
        changes.push({from: node.from + m[1]!.length, to: node.from + m[0].length, insert: String(prev + 2 + offset)})
      }
      prev = number
    }
    let next = node.nextSibling
    if (!next) break
    node = next
  }
}

function normalizeIndent(content: string, state: EditorState) {
  let blank = /^[ \t]*/.exec(content)![0].length
  if (!blank || state.facet(indentUnit) != "\t") return content
  let col = countColumn(content, 4, blank)
  let space = ""
  for (let i = col; i > 0;) {
    if (i >= 4) { space += "\t"; i -= 4 }
    else { space += " "; i-- }
  }
  return space + content.slice(blank)
}

/// This command, when invoked in Markdown context with cursor
/// selection(s), will create a new line with the markup for
/// blockquotes and lists that were active on the old line. If the
/// cursor was directly after the end of the markup for the old line,
/// trailing whitespace and list markers are removed from that line.
///
/// The command does nothing in non-Markdown context, so it should
/// not be used as the only binding for Enter (even in a Markdown
/// document, HTML and code regions might use a different language).
export const insertNewlineContinueMarkup: StateCommand = ({state, dispatch}) => {
  let tree = syntaxTree(state), {doc} = state
  let dont: {range: SelectionRange}|null = null, changes = state.changeByRange(range => {
    if (!range.empty || !markdownLanguage.isActiveAt(state, range.from, -1) && !markdownLanguage.isActiveAt(state, range.from, 1)) {
      return dont = {range}
    }
    let pos = range.from, line = doc.lineAt(pos)
    let context = getContext(tree.resolveInner(pos, -1), doc)
    while (context.length && context[context.length - 1]!.from > pos - line.from) context.pop()
    if (!context.length) return dont = {range}
    let inner = context[context.length - 1]!
    if (inner.to - inner.spaceAfter.length > pos - line.from) return dont = {range}

    let emptyLine = pos >= (inner.to - inner.spaceAfter.length) && !/\S/.test(line.text.slice(inner.to))
    // Empty line in list
    if (inner.item && emptyLine) {
      // let first = inner.node.firstChild, second = inner.node.getChild("listItem", "listItem")
      // // Not second item or blank line before: delete a level of markup
      // if (first.to >= pos || second && second.to < pos ||
          // line.from > 0 && !/[^\s>]/.test(doc.lineAt(line.from - 1).text)) {
      let next = context.length > 1 ? context[context.length - 2] : null
      let delTo, insert = ""
      if (next && next.item) { // Re-add marker for the list at the next level
        delTo = line.from + next.from
        insert = next.marker(doc, 1)
      } else {
        delTo = line.from + (next ? next.to : 0)
      }
      let changes = [{from: delTo, to: pos, insert}]
      if (inner.node.name == "listOrdered") {
        renumberList(inner.item, doc, changes, -2);
      }
      if (next && next.node.name == "listOrdered") {
        renumberList(next.item!, doc, changes);
      }
      return {range: EditorSelection.cursor(delTo + insert.length), changes}
      // } else { // Move second item down, making tight two-item list non-tight
      //   let insert = blankLine(context, state, line)
      //   return {range: EditorSelection.cursor(pos + insert.length + 1),
      //           changes: {from: line.from, insert: insert + state.lineBreak}}
      // }
    }

    if (inner.node.name == "blockQuote" && emptyLine && line.from) {
      let prevLine = doc.lineAt(line.from - 1), quoted = />\s*$/.exec(prevLine.text)
      // Two aligned empty quoted lines in a row
      if (quoted && quoted.index == inner.from) {
        let changes = state.changes([{from: prevLine.from + quoted.index, to: prevLine.to},
                                     {from: line.from + inner.from, to: line.to}])
        return {range: range.map(changes), changes}
      }
    }

    let changes: ChangeSpec[] = []
    if (inner.node.name == "listOrdered") {
      renumberList(inner.item!, doc, changes);
    }
    let continued = inner.item && inner.item.from < line.from;
    let insert = "";
    // If not dedented
    if (!continued || /^[\s\d.)\-+*>]*/.exec(line.text)![0].length >= inner.to) {
      for (let i = 0, e = context.length - 1; i <= e; i++) {
        insert += i == e && !continued ? context[i]!.marker(doc, 1)
          : context[i]!.blank(i < e ? countColumn(line.text, 4, context[i + 1]!.from) - insert.length : null)
      }
    }
    let from = pos;
    while (from > line.from && /\s/.test(line.text.charAt(from - line.from - 1))) { 
      from--; 
    }
    insert = normalizeIndent(insert, state)
    if (nonTightList(inner.node, state.doc)) {
      insert = blankLine(context, state, line) + state.lineBreak + insert;
    }
    changes.push({from, to: pos, insert: state.lineBreak + insert})
    return {range: EditorSelection.cursor(from + insert.length + 1), changes}
  })
  if (dont) { return false; }
  dispatch(state.update(changes, {scrollIntoView: true, userEvent: "input"}))
  return true
}


function nonTightList(node: SyntaxNode, doc: Text) {
  if (node.name != "listOrdered" && node.name != "listUnordered") return false
  let first = node.firstChild!, second = node.getChild("listItem", "listItem")
  if (!second) return false
  let line1 = doc.lineAt(first.to), line2 = doc.lineAt(second.from)
  let empty = /^[\s>]*$/.test(line1.text)
  return line1.number + (empty ? 0 : 1) < line2.number
}

function blankLine(context: Context[], state: EditorState, line: Line) {
  let insert = ""
  for (let i = 0, e = context.length - 2; i <= e; i++) {
    insert += context[i]!.blank(i < e 
      ? countColumn(line.text, 4, context[i + 1]!.from) - insert.length
      : null, i < e)
  }
  return normalizeIndent(insert, state)
}

function isMark(node: SyntaxNode) {
  return node.name == "blockQuotePrefix" || node.name == "listItemPrefix"
}

function contextNodeForDelete(tree: Tree, pos: number) {
  let node = tree.resolveInner(pos, -1), scan = pos
  if (isMark(node)) {
    scan = node.from
    node = node.parent!
  }
  for (let prev; prev = node.childBefore(scan);) {
    if (isMark(prev)) {
      scan = prev.from
    } else if (prev.name == "listOrdered" || prev.name == "listUnordered") {
      node = prev.lastChild!
      scan = node.to
    } else {
      break
    }
  }
  return node;
}


/// This command will, when invoked in a Markdown context with the
/// cursor directly after list or blockquote markup, delete one level
/// of markup. When the markup is for a list, it will be replaced by
/// spaces on the first invocation (a further invocation will delete
/// the spaces), to make it easy to continue a list.
///
/// When not after Markdown block markup, this command will return
/// false, so it is intended to be bound alongside other deletion
/// commands, with a higher precedence than the more generic commands.
export const deleteMarkupBackward: StateCommand = ({state, dispatch}) => {
  let tree = syntaxTree(state)
  let dont: {range: SelectionRange}|null = null, changes = state.changeByRange(range => {
    let pos = range.from, {doc} = state
    if (range.empty && markdownLanguage.isActiveAt(state, range.from)) {
      let line = doc.lineAt(pos)
      let context = getContext(contextNodeForDelete(tree, pos), doc)
      if (context.length) {
        let inner = context[context.length - 1]!
        let spaceEnd = inner.to - inner.spaceAfter.length + (inner.spaceAfter ? 1 : 0)
        // Delete extra trailing space after markup
        if (pos - line.from > spaceEnd && !/\S/.test(line.text.slice(spaceEnd, pos - line.from)))
          return {range: EditorSelection.cursor(line.from + spaceEnd),
                  changes: {from: line.from + spaceEnd, to: pos}}
        if (pos - line.from == spaceEnd &&
            // Only apply this if we're on the line that has the
            // construct's syntax, or there's only indentation in the
            // target range
            (!inner.item || line.from <= inner.item.from || !/\S/.test(line.text.slice(0, inner.to)))) {
          let start = line.from + inner.from
          // Replace a list item marker with blank space
          if (inner.item && inner.node.from < inner.item.from && /\S/.test(line.text.slice(inner.from, inner.to))) {
            let insert = inner.blank(countColumn(line.text, 4, inner.to) - countColumn(line.text, 4, inner.from))
            if (start == line.from) insert = normalizeIndent(insert, state)
            return {range: EditorSelection.cursor(start + insert.length),
                    changes: {from: start, to: line.from + inner.to, insert}}
          }
          // Delete one level of indentation
          if (start < pos) {
            return {range: EditorSelection.cursor(start), changes: {from: start, to: pos}};
          }
        }
      }
    }
    return dont = {range}
  })
  if (dont) return false
  dispatch(state.update(changes, {scrollIntoView: true, userEvent: "delete"}))
  return true
}


export function isTypeInSelection(state: EditorState|null|undefined, type: string) {
  if (!state) {
    return false;
  }
  let tree = syntaxTree(state);
  return state.selection.ranges.some(range => getIntersectionNodes(tree, range, n => n.name === type).length > 0);
}


function toggleMarkdownAction(
  {state, dispatch}: {state: EditorState, dispatch: (tr: Transaction) => void}, 
  { isInSelection, enable, disable }: { 
    isInSelection: (node: SyntaxNode) => boolean, 
    enable?: (range: SelectionRange, tree: Tree) => { range: SelectionRange, changes: ChangeSpec[] }, 
    disable?: (range: SelectionRange, foundNodes: SyntaxNode[]) => { range: SelectionRange, changes: ChangeSpec[] }
  }) {
  const tree = syntaxTree(state);

  const changes = state.changeByRange(range => {
    if (!markdownLanguage.isActiveAt(state, range.from)) {
      return {range};
    }

    const foundNodes = getIntersectionNodes(tree, range, isInSelection);
    if (foundNodes.length > 0) {
      if (disable) {
        return disable(range, foundNodes);
      } else {
        return {range};
      }
    } else {
      if (enable) {
        return enable(range, tree);
      } else {
        return {range};
      }
    }
  });

  dispatch(state.update(changes, {scrollIntoView: true, userEvent: 'input'}));
  return true;
}


function toggleMarkerType({state, dispatch}: CommandArg, { type, markerTypes, startMarker, endMarker}: {
  type: string,
  markerTypes: string[],
  startMarker: string,
  endMarker: string
}) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => {
      return n.name === type || (n.name === 'data' && state.doc.sliceString(n.from, n.to) === startMarker + endMarker)
    },
    enable: (range) => {
      if (range.empty) {
        const insertText = 'text';
        return {
          range: EditorSelection.range(range.from + startMarker.length, range.to + startMarker.length + insertText.length),
          changes: [{from: range.from, insert: startMarker + insertText + endMarker}],
        };
      } else {
        // insert bold markers at start and end of selection
        return {
          range: EditorSelection.range(range.from + startMarker.length, range.to + startMarker.length),
          changes: [
            {from: range.from, insert: startMarker},
            {from: range.to, insert: endMarker},
        ]};
      }
    },
    disable: (range, foundNodes) => {
      // remove bold markers of all intersecting bold nodes
      const removeMarkers = foundNodes
        .flatMap(n => getChildren(n).concat(n))
        .filter(c => markerTypes.includes(c.name) || (c.name === 'data' && state.doc.sliceString(c.from, c.to) === startMarker + endMarker));
      let newRange = range;
      const changes: ChangeSpec[] = [];
      for (const cn of removeMarkers) {
        const change = {from: cn.from, to: cn.to};
        newRange = moveRangeDelete(newRange, range, change)
        changes.push(change);
      }
      return { range: newRange, changes };
    }
  });
}

export function toggleStrong({state, dispatch}: CommandArg) {
  return toggleMarkerType({state, dispatch}, {
    type: 'strong',
    markerTypes: ['strongSequence'],
    startMarker: '**',
    endMarker: '**'
  });
}

export function toggleEmphasis({state, dispatch}: CommandArg) {
  return toggleMarkerType({state, dispatch}, {
    type: 'emphasis',
    markerTypes: ['emphasisSequence'],
    startMarker: '_',
    endMarker: '_',
  });
}

export function toggleStrikethrough({state, dispatch}: CommandArg) {
  return toggleMarkerType({state, dispatch}, {
    type: 'strikethrough',
    markerTypes: ['strikethroughSequence'],
    startMarker: '~~',
    endMarker: '~~'
  });
}

export function toggleFootnote({state, dispatch}: CommandArg) {
  return toggleMarkerType({state, dispatch}, {
    type: 'inlineFootnote',
    markerTypes: ['inlineFootnoteMarker', 'inlineFootnoteStartMarker', 'inlineFootnoteEndMarker'],
    startMarker: '^[',
    endMarker: ']'
  });
}

export function toggleListUnordered({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'listUnordered',
    enable: (range, tree) => {
      // Add marker to start of each line
      // If line is a listItem of an listOrdered: replace the marker
      const changes: ChangeSpec[] = [];
      let newRange = range;
      for (const line of linesInRange(state.doc, range)) {
        const listItemNumber = getIntersectionNodes(tree, line, n => n.name === 'listItem' && n.parent!.name === 'listOrdered')
          .flatMap(n => getChildren(n))
          .filter(n => n.name === 'listItemPrefix')
          .find(n => intersectsRange(line, n));
        
        if (listItemNumber) {
          const change = {from: listItemNumber.from, to: listItemNumber.to, insert: '* '};
          newRange = moveRangeInsert(moveRangeDelete(newRange, range, change), range, change);
          changes.push(change);
        } else {
          const change = {from: line.from, insert: '* '};
          newRange = moveRangeInsert(newRange, range, change);
          changes.push(change);
        }
      }
      return {
        range: newRange,
        changes,
      };
    },
    disable: (range, foundNodes) => {
      const removeMarkers = foundNodes
        .flatMap(n => getChildren(n))  // Get all listItems
        .filter(i => intersectsRange(range, i))  // Get selected listItems
        .flatMap(n => getChildren(n))
        .filter(n => n.name === 'listItemPrefix');
      let newRange = range;
      const changes: ChangeSpec[] = [];
      for (const cn of removeMarkers) {
        const change = {from: cn.from, to: cn.to};
        newRange = moveRangeDelete(newRange, range, change)
        changes.push(change);
      }
      return { range: newRange, changes };
    }
  });
}

export function toggleListOrdered({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'listOrdered',
    enable: (range, tree) => {
      // Add marker to start of each line
      // If line is a listItem of an listUnordered: replace the marker
      const changes: ChangeSpec[] = [];
      let newRange = range;
      let itemNumber = 0;
      for (const line of linesInRange(state.doc, range)) {
        itemNumber += 1;
        const listItemNumber = itemNumber + '. ';

        const listItemBullet = getIntersectionNodes(tree, line, n => n.name === 'listItem' && n.parent!.name === 'listUnordered')
          .flatMap(n => getChildren(n))
          .filter(n => n.name === 'listItemPrefix')
          .find(n => intersectsRange(line, n));
        
        if (listItemBullet) {
          const change = {from: listItemBullet.from, to: listItemBullet.to, insert: listItemNumber};
          newRange = moveRangeInsert(moveRangeDelete(newRange, range, change), range, change);
          changes.push(change);
        } else {
          const change = {from: line.from, insert: listItemNumber};
          newRange = moveRangeInsert(newRange, range, change);
          changes.push(change);
        }
      }
      return {
        range: newRange,
        changes,
      };
    },
    disable: (range, foundNodes) => {
      const removeMarkers = foundNodes
        .flatMap(n => getChildren(n))  // Get all listItems
        .filter(i => intersectsRange(range, i))  // Get selected listItems
        .flatMap(n => getChildren(n))
        .filter(n => n.name === 'listItemPrefix');
      let newRange = range;
      const changes: ChangeSpec[] = [];
      for (const cn of removeMarkers) {
        const change = {from: cn.from, to: cn.to};
        newRange = moveRangeDelete(newRange, range, change)
        changes.push(change);
      }
      return { range: newRange, changes };
    }
  });
}

export function toggleBlockQuote({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'blockQuote',
    enable: (range) => {
      // Add '> ' to the start of each line in the selection
      const changes: ChangeSpec[] = [];
      let newRange = range;
      for (const line of linesInRange(state.doc, range)) {
        const change = {from: line.from, insert: '> '};
        newRange = moveRangeInsert(newRange, range, change);
        changes.push(change);
      }
      return {
        range: newRange,
        changes,
      };
    },
    disable: (range, foundNodes) => {
      const selectedLines = linesInRange(state.doc, range);
      const lineRange = selectedLines.length > 0 ? {from: selectedLines[0]!.from, to: selectedLines.at(-1)!.to} : range;

      // Remove blockquote markers
      const removeMarkers = foundNodes.flatMap(n => getChildren(n)).flatMap(n => {
        if (n.name === 'blockQuotePrefix') {
          return {from: n.from, to: n.to};
        } else if (n.name !== 'lineEnding') {
          return Array.from(state.doc.sliceString(n.from, n.to).matchAll(/\n> /g))
            .map(m => ({from: n.from + m.index + 1, to: n.from + m.index + m[0].length}));
        } else {
          return [];
        }
      })
      .filter(r => !range.empty ? intersectsRange(lineRange, r) : true);
      
      let newRange = range;
      const changes: ChangeSpec[] = [];
      for (const change of removeMarkers) {
        newRange = moveRangeDelete(newRange, range, change)
        changes.push(change);
      }
      return { range: newRange, changes };
    }
  });
}

function isTaskListItem(node: SyntaxNode, doc: Text) {
  const contentNode = node.firstChild?.nextSibling;
  if (node.name !== 'listItem' || node.firstChild?.name !== 'listItemPrefix' || !contentNode) {
    return false;
  }
  const content = doc.slice(contentNode.from, contentNode.to).toString() || '';
  return content.startsWith('[ ]') || content.startsWith('[x]');
}

function isTaskList(node: SyntaxNode, doc: Text) {
  return node.name === 'listUnordered' && getChildren(node).some(c => isTaskListItem(c, doc));
}

export function isTaskListInSelection(state?: EditorState|null) {
  if (!state) {
    return false;
  }
  let tree = syntaxTree(state);
  return state.selection.ranges.some(range => getIntersectionNodes(tree, range, n => isTaskList(n, state.doc)).length > 0);
}

export function toggleTaskList({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => isTaskList(n, state.doc),
    enable: (range, tree) => {
      // Add marker to start of each line
      // If line is a listItem of an listOrdered: replace the marker
      const changes: ChangeSpec[] = [];
      let newRange = range;
      for (const line of linesInRange(state.doc, range)) {
        const listItemNumber = getIntersectionNodes(tree, line, n => n.name === 'listItem' && !isTaskListItem(n, state.doc))
          .flatMap(n => getChildren(n))
          .filter(n => n.name === 'listItemPrefix')
          .find(n => intersectsRange(line, n));
        
        if (listItemNumber) {
          const change = {from: listItemNumber.from, to: listItemNumber.to, insert: '* [ ] '};
          newRange = moveRangeInsert(moveRangeDelete(newRange, range, change), range, change);
          changes.push(change);
        } else {
          const change = {from: line.from, insert: '* [ ] '};
          newRange = moveRangeInsert(newRange, range, change);
          changes.push(change);
        }
      }
      return {
        range: newRange,
        changes,
      };
    },
    disable: (range, foundNodes) => {
      const removeMarkers = foundNodes
        .flatMap(n => getChildren(n))  // Get all listItems
        .filter(i => intersectsRange(range, i))  // Get selected listItems
        .flatMap(n => getChildren(n))
        .map(n => {
          if (n.name === 'listItemPrefix') {
            return n;
          }
          const taskListCheck = (state.doc.slice(n.from, n.to).toString() || '').match(/^(?<check>\[[ |x]\]\s*)/)?.groups!.check;
          if (taskListCheck) {
            // Remove taskListCheck and the following space
            return {from: n.from, to: n.from + taskListCheck.length};
          }
          return null;
        })
        .filter(n => !!n);
      let newRange = range;
      const changes: ChangeSpec[] = [];
      for (const cn of removeMarkers) {
        const change = {from: cn!.from, to: cn!.to};
        newRange = moveRangeDelete(newRange, range, change)
        changes.push(change);
      }
      return { range: newRange, changes };
    }
  });
}


export function toggleLink({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'link',
    enable: (range) => {
      return {
        range: EditorSelection.range(range.from + 1, range.to + 1),
        changes: [
          {from: range.from, insert: '['},
          {from: range.to, insert: '](' + (range.from === range.to ? 'https://' : state.doc.sliceString(range.from, range.to)) + ')'},
        ]
      };
    },
    disable: (range, foundNodes) => {
      // Remove links only when a range is inside the link label
      const linksToRemove = foundNodes
        .filter(n => 
          getChildren(n)
          .flatMap(c => getChildren(c))
          .filter(c => c.name === 'labelText' && c.parent!.name === 'label' && range.from >= c.from && range.to <= c.to)
          .length === 1)
        .flatMap(n => getChildren(n).flatMap(c => getChildren(c)))
        .filter(c => !(c.name === 'labelText' && c.parent!.name === 'label'));
      const changes: ChangeSpec[] = [];
      let newRange = range;
      for (const n of linksToRemove) {
        const change = {from: n.from, to: n.to};
        newRange = moveRangeDelete(newRange, range, change);
        changes.push(change);
      }
      return {
        range: newRange,
        changes,
      };
    }
  });
}


export function insertText({state, dispatch}: CommandArg, text: string) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: () => false,
    enable: (range) => {
      return {
        range: EditorSelection.cursor(range.to + text.length),
        changes: [{from: range.to, insert: text}],
      };
    }
  });
}


export function insertCodeBlock({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'codeFenced',
    enable: (range) => {
      // insert "```\n" at the start of the first selected line and "\n```" at the end of the last selected line
      const codeBlockStart = state.lineBreak + '```' + state.lineBreak;
      const codeBlockEnd = state.lineBreak + '```';
      return {
        range: EditorSelection.range(range.from + codeBlockStart.length, range.to + codeBlockStart.length),
        changes: [
          {from: state.doc.lineAt(range.from).from, insert: codeBlockStart},
          {from: state.doc.lineAt(range.to).to, insert: codeBlockEnd},
        ],
      };
    },
  })
}

export function insertTable({state, dispatch}: CommandArg) {
  return toggleMarkdownAction({state, dispatch}, {
    isInSelection: n => n.name === 'table',
    enable: (range) => {
      return {
        range,
        changes: [{
          from: state.doc.lineAt(range.to).to, 
          insert: state.lineBreak +
                  '| Column1 | Column2 | Column3 |' + state.lineBreak +
                  '| ------- | ------- | ------- |' + state.lineBreak +
                  '| Text    | Text    | Text    |' + state.lineBreak + 
                  state.lineBreak,
        }],
      };
    }
  });
}

