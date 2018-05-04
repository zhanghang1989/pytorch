#pragma once
#include "lexer.h"
#include "tree.h"
#include "tree_views.h"

namespace torch {
namespace jit {
namespace script {

struct Parser {
  explicit Parser(const std::string& str)
      : L(str), shared(sharedParserData()) {}

  Ident parseIdent() {
    auto t = L.expect(TK_IDENT);
    // whenever we parse something that has a TreeView type we always
    // use its create method so that the accessors and the constructor
    // of the Compound tree are in the same place.
    return Ident::create(t.range, t.text());
  }
  TreeRef createApply(Expr expr) {
    TreeList attributes;
    auto range = L.cur().range;
    TreeList inputs;
    parseOperatorArguments(inputs, attributes);
    return Apply::create(
        range,
        expr,
        List<Expr>(makeList(range, std::move(inputs))),
        List<Attribute>(makeList(range, std::move(attributes))));
  }
  // things like a 1.0 or a(4) that are not unary/binary expressions
  // and have higher precedence than all of them
  TreeRef parseBaseExp() {
    TreeRef prefix;
    switch (L.cur().kind) {
      case TK_NUMBER:
      case TK_TRUE:
      case TK_FALSE: {
        prefix = parseConst();
      } break;
      case '(': {
        L.next();
        prefix = parseExp();
        L.expect(')');
      } break;
      case TK_FLOAT:
      case TK_INT:
      case TK_LONG: {
        auto r = L.cur().range;
        auto type = c(L.next().kind, r, {});
        L.expect('(');
        auto exp = parseExp();
        L.expect(')');
        prefix = Cast::create(r, Type(type), Expr(exp));
      } break;
      default: {
        Ident name = parseIdent();
        prefix = Var::create(name.range(), name);
      } break;
    }
    while (true) {
      if (L.nextIf('.')) {
        const auto name = parseIdent();
        prefix = Select::create(name.range(), Expr(prefix), Ident(name));
      } else if (L.cur().kind == '(') {
        prefix = createApply(Expr(prefix));
      } else if (L.cur().kind == '[') {
        prefix = parseSliceOrGather(prefix);
      } else {
        break;
      }
    }
    return prefix;
  }
  TreeRef parseOptionalReduction() {
    auto r = L.cur().range;
    switch (L.cur().kind) {
      case TK_PLUS_EQ:
      case TK_MINUS_EQ:
      case TK_TIMES_EQ:
      case TK_DIV_EQ: {
        int modifier = L.next().text()[0];
        return c(modifier, r, {});
      } break;
      default: {
        L.expect('=');
        return c('=', r, {}); // no reduction
      } break;
    }
  }
  TreeRef
  parseTrinary(TreeRef true_branch, const SourceRange& range, int binary_prec) {
    auto cond = parseExp();
    L.expect(TK_ELSE);
    auto false_branch = parseExp(binary_prec);
    return c(TK_IF_EXPR, range, {cond, true_branch, false_branch});
  }
  // parse the longest expression whose binary operators have
  // precedence strictly greater than 'precedence'
  // precedence == 0 will parse _all_ expressions
  // this is the core loop of 'top-down precedence parsing'
  Expr parseExp() { return parseExp(0); }
  Expr parseExp(int precedence) {
    TreeRef prefix = nullptr;
    int unary_prec;
    if (shared.isUnary(L.cur().kind, &unary_prec)) {
      auto kind = L.cur().kind;
      auto pos = L.cur().range;
      L.next();
      prefix = c(kind, pos, {parseExp(unary_prec)});
    } else {
      prefix = parseBaseExp();
    }
    int binary_prec;
    while (shared.isBinary(L.cur().kind, &binary_prec)) {
      if (binary_prec <= precedence) // not allowed to parse something which is
        // not greater than 'precedenc'
        break;

      int kind = L.cur().kind;
      auto pos = L.cur().range;
      L.next();
      if (shared.isRightAssociative(kind))
        binary_prec--;

      // special case for trinary operator
      if (kind == TK_IF) {
        prefix = parseTrinary(prefix, pos, binary_prec);
        continue;
      }

      prefix = c(kind, pos, {prefix, parseExp(binary_prec)});
    }
    return Expr(prefix);
  }
  template<typename T>
  List<T> parseList(int begin, int sep, int end, T (Parser::*parse)()) {
    auto r = L.cur().range;
    if (begin != TK_NOTHING)
      L.expect(begin);
    std::vector<T> elements;
    if (L.cur().kind != end) {
      do {
        elements.push_back((this->*parse)());
      } while (L.nextIf(sep));
    }
    if (end != TK_NOTHING)
      L.expect(end);
    return List<T>::create(r, elements);
  }
  TreeRef parseConst() {
    // 'b' - boolean
    // 'LL' 64-bit integer
    // 'f' single-precision float
    // 'i' 32-bit integer
    // 'f' is default if '.' appears in the number
    auto range = L.cur().range;
    if (L.nextIf(TK_TRUE)) {
      return c(TK_CONST, range, {d(1), s("b")});
    } else if (L.nextIf(TK_FALSE)) {
      return c(TK_CONST, range, {d(0), s("b")});
    }
    float mult = 1.0f;
    while (L.nextIf('-')) {
      mult *= -1.0f;
    }
    auto t = L.expect(TK_NUMBER);
    std::string type_ident =
        (t.text().find('.') == std::string::npos) ? "i" : "f";
    if (L.cur().kind == TK_IDENT) {
      Token type_ident_tok = L.expect(TK_IDENT);
      type_ident = type_ident_tok.text();
      if (type_ident != "LL" && type_ident != "f") {
        throw ErrorReport(type_ident_tok)
            << "expected 'f' or 'LL' "
            << "as numeric type identifier but found '" << type_ident << "'";
      }
    }
    return c(TK_CONST, t.range, {d(mult * t.doubleValue()), s(type_ident)});
  }
  TreeRef parseAttributeValue() {
    int kind = L.cur().kind;
    switch (kind) {
      case '[': {
        auto list = parseList('[', ',', ']', &Parser::parseConst);
        return ListLiteral::create(list.range(), List<Expr>(list));
      } default:
        return parseConst();
    }
  }
  void parseOperatorArguments(TreeList& inputs, TreeList& attributes) {
    L.expect('(');
    if (L.cur().kind != ')') {
      do {
        if (L.cur().kind == TK_IDENT && L.lookahead().kind == '=') {
          auto ident = parseIdent();
          L.expect('=');
          auto v = parseAttributeValue();
          attributes.push_back(Attribute::create(ident.range(), Ident(ident), v));
        } else {
          inputs.push_back(parseExp());
        }
      } while (L.nextIf(','));
    }
    L.expect(')');
  }

  // OK: [a] (gather), [a:], [:a], [a:b], [:] (slice)
  // Not OK: []
  TreeRef parseSliceOrGather(TreeRef value) {
    const auto range = L.cur().range;
    L.expect('[');

    // `first` will either be the gather indices, or the start of the slice.
    TreeRef first, second;

    // Here we can either have a colon (which starts a slice), or an expression.
    // If an expression, we don't know yet if it will be a slice or a gather.
    if (L.cur().kind != ':') {
      first = parseExp();
      if (L.nextIf(']')) {
        return Gather::create(range, Expr(value), Expr(first));
      } else {
        first = c(TK_OPTION, range, {first});
      }
    } else {
      first = c(TK_OPTION, range, {});
    }
    L.expect(':');
    // Now we *may* have an expression.
    if (L.cur().kind != ']') {
      second = c(TK_OPTION, range, {parseExp()});
    } else {
      second = c(TK_OPTION, range, {});
    }
    L.expect(']');

    return Slice::create(range, Expr(value), Maybe<Expr>(first), Maybe<Expr>(second));
  }
  TreeRef parseParam() {
    auto typ = parseType();
    if (L.cur().kind != TK_IDENT && typ->trees()[0]->kind() == TK_IDENT) {
      // oops, it wasn't a type but just a param without any type specified
      return Param::create(
          typ->range(), Ident(typ->trees()[0]), Type(c(TK_INFERRED, typ->range(), {})));
    }
    auto ident = parseIdent();
    return Param::create(typ->range(), Ident(ident), Type(typ));
  }
  // TODO: these functions should be unnecessary, but we currently do not
  // emit a TK_NEWLINE before a series of TK_DEDENT tokens
  // so if we see a TK_DEDENT then we know a newline must have happened and
  // ignore it. The real fix is to patch the lexer so TK_NEWLINE does get
  // emited before a TK_INDENT
  void expectEndOfLine() {
    if (L.cur().kind != TK_DEDENT)
      L.expect(TK_NEWLINE);
  }
  bool isEndOfLine() {
    return L.cur().kind == TK_NEWLINE || L.cur().kind == TK_DEDENT;
  }

  // 'first' has already been parsed since expressions can exist
  // alone on a line:
  // first[,other,lhs] = rhs
  Assign parseAssign(Ident first) {
    List<Ident> list = parseOneOrMoreIdents(first);
    auto red = parseOptionalReduction();
    auto rhs = parseExp();
    expectEndOfLine();
    return Assign::create(list.range(), list, AssignKind(red), Expr(rhs));
  }
  TreeRef parseStmt() {
    switch (L.cur().kind) {
      case TK_IF:
        return parseIf();
      case TK_WHILE:
        return parseWhile();
      case TK_GLOBAL: {
        auto range = L.next().range;
        auto idents = parseList(TK_NOTHING, ',', TK_NOTHING, &Parser::parseIdent);
        return Global::create(range, idents);
      }
      case TK_RETURN: {
        auto range = L.next().range;
        auto values = parseList(TK_NOTHING, ',', TK_NOTHING, &Parser::parseExp);
        return Return::create(range, values);
      }
      default: {
        Expr r = Expr(parseExp());
        if (r.kind() == TK_VAR && !isEndOfLine()) {
          return parseAssign(Var(r).name());
        } else {
          expectEndOfLine();
          return ExprStmt::create(r.range(), r);
        }
      }
    }
  }
  TreeRef parseScalarType() {
    switch (L.cur().kind) {
      case TK_INT:
      case TK_FLOAT:
      case TK_LONG:
      case TK_DOUBLE: {
        auto t = L.next();
        return c(t.kind, t.range, {});
      }
      default:
        return parseIdent();
    }
  }
  TreeRef parseOptionalIdentList() {
    TreeRef list = nullptr;
    if (L.cur().kind == '(') {
      list = parseList('(', ',', ')', &Parser::parseIdent);
    } else {
      list = c(TK_LIST, L.cur().range, {});
    }
    return list;
  }
  TreeRef parseType() {
    return TensorType::create(SourceRange(std::make_shared<std::string>(""), 0, 0));
  }
  // 'first' has already been parsed, add the rest
  // if they exist
  // first[, the, rest]
  List<Ident> parseOneOrMoreIdents(Ident first) {
    std::vector<Ident> idents {first};
    while (L.nextIf(',')) {
      idents.push_back(parseIdent());
    }
    return List<Ident>::create(idents.back().range(), std::move(idents));
  }
  TreeRef parseIf() {
    auto r = L.cur().range;
    L.expect(TK_IF);
    auto cond = parseExp();
    L.expect(':');
    auto true_branch = parseStatements();
    auto false_branch = makeList(L.cur().range, {});
    if (L.nextIf(TK_ELSE)) {
      L.expect(':');
      false_branch = parseStatements();
    }
    return If::create(r, Expr(cond), List<Stmt>(true_branch), List<Stmt>(false_branch));
  }
  TreeRef parseWhile() {
    auto r = L.cur().range;
    L.expect(TK_WHILE);
    auto cond = parseExp();
    L.expect(':');
    auto body = parseStatements();
    return While::create(r, Expr(cond), List<Stmt>(body));
  }
  TreeRef parseStatements() {
    auto r = L.cur().range;
    L.expect(TK_INDENT);
    TreeList stmts;
    while (true) {
      stmts.push_back(parseStmt());
      if (L.nextIf(TK_DEDENT))
        break;
    }
    return c(TK_LIST, r, std::move(stmts));
  }
  TreeRef parseFunction() {
    L.expect(TK_DEF);
    auto name = parseIdent();
    auto paramlist = parseList('(', ',', ')', &Parser::parseParam);
    L.expect(':');
    auto stmts_list = parseStatements();
    return Def::create(name.range(), Ident(name), List<Param>(paramlist),
                       List<Stmt>(stmts_list));
  }
  Lexer& lexer() {
    return L;
  }

 private:
  // short helpers to create nodes
  TreeRef d(double v) {
    return Number::create(v);
  }
  TreeRef s(const std::string& s) {
    return String::create(s);
  }
  TreeRef c(int kind, const SourceRange& range, TreeList&& trees) {
    return Compound::create(kind, range, std::move(trees));
  }
  TreeRef makeList(const SourceRange& range, TreeList&& trees) {
    return c(TK_LIST, range, std::move(trees));
  }
  Lexer L;
  SharedParserData& shared;
};
} // namespace script
} // namespace jit
} // namespace torch
